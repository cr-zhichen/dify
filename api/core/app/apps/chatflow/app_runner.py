import logging
import os
import time
from collections.abc import Generator, Mapping
from typing import Any, Optional, cast

from core.app.apps.advanced_chat.app_config_manager import AdvancedChatAppConfig
from core.app.apps.base_app_queue_manager import AppQueueManager, PublishFrom
from core.app.apps.base_app_runner import AppRunner
from core.app.apps.workflow_logging_callback import WorkflowLoggingCallback
from core.app.entities.app_invoke_entities import (
    AdvancedChatAppGenerateEntity,
    InvokeFrom,
)
from core.app.entities.queue_entities import (
    AppQueueEvent,
    QueueAnnotationReplyEvent,
    QueueIterationCompletedEvent,
    QueueIterationNextEvent,
    QueueIterationStartEvent,
    QueueNodeFailedEvent,
    QueueNodeStartedEvent,
    QueueNodeSucceededEvent,
    QueueParallelBranchRunFailedEvent,
    QueueParallelBranchRunStartedEvent,
    QueueRetrieverResourcesEvent,
    QueueStopEvent,
    QueueTextChunkEvent,
    QueueWorkflowFailedEvent,
    QueueWorkflowStartedEvent,
    QueueWorkflowSucceededEvent,
)
from core.moderation.base import ModerationException
from core.workflow.callbacks.base_workflow_callback import WorkflowCallback
from core.workflow.entities.node_entities import SystemVariable, UserFrom
from core.workflow.graph_engine.entities.event import (
    GraphRunFailedEvent,
    GraphRunStartedEvent,
    GraphRunSucceededEvent,
    IterationRunFailedEvent,
    IterationRunNextEvent,
    IterationRunStartedEvent,
    IterationRunSucceededEvent,
    NodeRunFailedEvent,
    NodeRunRetrieverResourceEvent,
    NodeRunStartedEvent,
    NodeRunStreamChunkEvent,
    NodeRunSucceededEvent,
    ParallelBranchRunFailedEvent,
    ParallelBranchRunStartedEvent,
    ParallelBranchRunSucceededEvent,
)
from core.workflow.workflow_entry import WorkflowEntry
from extensions.ext_database import db
from models.model import App, Conversation, EndUser, Message
from models.workflow import Workflow

logger = logging.getLogger(__name__)


class AdvancedChatAppRunner(AppRunner):
    """
    AdvancedChat Application Runner
    """

    def run(self, application_generate_entity: AdvancedChatAppGenerateEntity,
            queue_manager: AppQueueManager,
            conversation: Conversation,
            message: Message) -> Generator[AppQueueEvent, None, None]:
        """
        Run application
        :param application_generate_entity: application generate entity
        :param queue_manager: application queue manager
        :param conversation: conversation
        :param message: message
        :return:
        """
        app_config = application_generate_entity.app_config
        app_config = cast(AdvancedChatAppConfig, app_config)

        app_record = db.session.query(App).filter(App.id == app_config.app_id).first()
        if not app_record:
            raise ValueError("App not found")

        workflow = self.get_workflow(app_model=app_record, workflow_id=app_config.workflow_id)
        if not workflow:
            raise ValueError("Workflow not initialized")

        inputs = application_generate_entity.inputs
        query = application_generate_entity.query
        files = application_generate_entity.files

        user_id = None
        if application_generate_entity.invoke_from in [InvokeFrom.WEB_APP, InvokeFrom.SERVICE_API]:
            end_user = db.session.query(EndUser).filter(EndUser.id == application_generate_entity.user_id).first()
            if end_user:
                user_id = end_user.session_id
        else:
            user_id = application_generate_entity.user_id

        # moderation
        if self.handle_input_moderation(
                queue_manager=queue_manager,
                app_record=app_record,
                app_generate_entity=application_generate_entity,
                inputs=inputs,
                query=query,
                message_id=message.id
        ):
            return

        # annotation reply
        if self.handle_annotation_reply(
                app_record=app_record,
                message=message,
                query=query,
                queue_manager=queue_manager,
                app_generate_entity=application_generate_entity
        ):
            return

        db.session.close()

        workflow_callbacks: list[WorkflowCallback] = []
        if bool(os.environ.get("DEBUG", 'False').lower() == 'true'):
            workflow_callbacks.append(WorkflowLoggingCallback())

        # RUN WORKFLOW
        workflow_entry = WorkflowEntry(
            workflow=workflow,
            user_id=application_generate_entity.user_id,
            user_from=UserFrom.ACCOUNT
            if application_generate_entity.invoke_from in [InvokeFrom.EXPLORE, InvokeFrom.DEBUGGER]
            else UserFrom.END_USER,
            invoke_from=application_generate_entity.invoke_from,
            user_inputs=inputs,
            system_inputs={
                SystemVariable.QUERY: query,
                SystemVariable.FILES: files,
                SystemVariable.CONVERSATION_ID: conversation.id,
                SystemVariable.USER_ID: user_id
            },
            call_depth=application_generate_entity.call_depth
        )
        generator = workflow_entry.run(
            callbacks=workflow_callbacks,
        )

        for event in generator:
            if isinstance(event, GraphRunStartedEvent):
                queue_manager.publish(
                    QueueWorkflowStartedEvent(),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, GraphRunSucceededEvent):
                queue_manager.publish(
                    QueueWorkflowSucceededEvent(),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, GraphRunFailedEvent):
                queue_manager.publish(
                    QueueWorkflowFailedEvent(error=event.error),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, NodeRunStartedEvent):
                queue_manager.publish(
                    QueueNodeStartedEvent(
                        node_id=event.node_id,
                        node_type=event.node_type,
                        node_data=event.node_data,
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        node_run_index=workflow_entry.graph_engine.graph_runtime_state.node_run_steps,
                        predecessor_node_id=event.predecessor_node_id
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, NodeRunSucceededEvent):
                queue_manager.publish(
                    QueueNodeSucceededEvent(
                        node_id=event.node_id,
                        node_type=event.node_type,
                        node_data=event.node_data,
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        inputs=event.route_node_state.node_run_result.inputs
                        if event.route_node_state.node_run_result else {},
                        process_data=event.route_node_state.node_run_result.process_data
                        if event.route_node_state.node_run_result else {},
                        outputs=event.route_node_state.node_run_result.outputs
                        if event.route_node_state.node_run_result else {},
                        execution_metadata=event.route_node_state.node_run_result.metadata
                        if event.route_node_state.node_run_result else {},
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, NodeRunFailedEvent):
                queue_manager.publish(
                    QueueNodeFailedEvent(
                        node_id=event.node_id,
                        node_type=event.node_type,
                        node_data=event.node_data,
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        inputs=event.route_node_state.node_run_result.inputs
                        if event.route_node_state.node_run_result else {},
                        process_data=event.route_node_state.node_run_result.process_data
                        if event.route_node_state.node_run_result else {},
                        outputs=event.route_node_state.node_run_result.outputs
                        if event.route_node_state.node_run_result else {},
                        error=event.route_node_state.node_run_result.error
                        if event.route_node_state.node_run_result
                        and event.route_node_state.node_run_result.error
                        else "Unknown error"
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, NodeRunStreamChunkEvent):
                queue_manager.publish(
                    QueueTextChunkEvent(
                        text=event.chunk_content
                    ), PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, NodeRunRetrieverResourceEvent):
                queue_manager.publish(
                    QueueRetrieverResourcesEvent(
                        retriever_resources=event.retriever_resources
                    ), PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, ParallelBranchRunStartedEvent):
                queue_manager.publish(
                    QueueParallelBranchRunStartedEvent(
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, ParallelBranchRunSucceededEvent):
                queue_manager.publish(
                    QueueParallelBranchRunStartedEvent(
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, ParallelBranchRunFailedEvent):
                queue_manager.publish(
                    QueueParallelBranchRunFailedEvent(
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        error=event.error
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, IterationRunStartedEvent):
                queue_manager.publish(
                    QueueIterationStartEvent(
                        node_id=event.iteration_node_id,
                        node_type=event.iteration_node_type,
                        node_data=event.iteration_node_data,
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        node_run_index=workflow_entry.graph_engine.graph_runtime_state.node_run_steps,
                        inputs=event.inputs,
                        predecessor_node_id=event.predecessor_node_id,
                        metadata=event.metadata
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, IterationRunNextEvent):
                queue_manager.publish(
                    QueueIterationNextEvent(
                        node_id=event.iteration_node_id,
                        node_type=event.iteration_node_type,
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        index=event.index,
                        node_run_index=workflow_entry.graph_engine.graph_runtime_state.node_run_steps,
                        output=event.pre_iteration_output,
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )
            elif isinstance(event, (IterationRunSucceededEvent | IterationRunFailedEvent)):
                queue_manager.publish(
                    QueueIterationCompletedEvent(
                        node_id=event.iteration_node_id,
                        node_type=event.iteration_node_type,
                        parallel_id=event.parallel_id,
                        parallel_start_node_id=event.parallel_start_node_id,
                        node_run_index=workflow_entry.graph_engine.graph_runtime_state.node_run_steps,
                        inputs=event.inputs,
                        outputs=event.outputs,
                        metadata=event.metadata,
                        steps=event.steps,
                        error=event.error if isinstance(event, IterationRunFailedEvent) else None
                    ),
                    PublishFrom.APPLICATION_MANAGER
                )

    def get_workflow(self, app_model: App, workflow_id: str) -> Optional[Workflow]:
        """
        Get workflow
        """
        # fetch workflow by workflow_id
        workflow = db.session.query(Workflow).filter(
            Workflow.tenant_id == app_model.tenant_id,
            Workflow.app_id == app_model.id,
            Workflow.id == workflow_id
        ).first()

        # return workflow
        return workflow

    def handle_input_moderation(
            self, queue_manager: AppQueueManager,
            app_record: App,
            app_generate_entity: AdvancedChatAppGenerateEntity,
            inputs: Mapping[str, Any],
            query: str,
            message_id: str
    ) -> bool:
        """
        Handle input moderation
        :param queue_manager: application queue manager
        :param app_record: app record
        :param app_generate_entity: application generate entity
        :param inputs: inputs
        :param query: query
        :param message_id: message id
        :return:
        """
        try:
            # process sensitive_word_avoidance
            _, inputs, query = self.moderation_for_inputs(
                app_id=app_record.id,
                tenant_id=app_generate_entity.app_config.tenant_id,
                app_generate_entity=app_generate_entity,
                inputs=inputs,
                query=query,
                message_id=message_id,
            )
        except ModerationException as e:
            self._stream_output(
                queue_manager=queue_manager,
                text=str(e),
                stream=app_generate_entity.stream,
                stopped_by=QueueStopEvent.StopBy.INPUT_MODERATION
            )
            return True

        return False

    def handle_annotation_reply(self, app_record: App,
                                message: Message,
                                query: str,
                                queue_manager: AppQueueManager,
                                app_generate_entity: AdvancedChatAppGenerateEntity) -> bool:
        """
        Handle annotation reply
        :param app_record: app record
        :param message: message
        :param query: query
        :param queue_manager: application queue manager
        :param app_generate_entity: application generate entity
        """
        # annotation reply
        annotation_reply = self.query_app_annotations_to_reply(
            app_record=app_record,
            message=message,
            query=query,
            user_id=app_generate_entity.user_id,
            invoke_from=app_generate_entity.invoke_from
        )

        if annotation_reply:
            queue_manager.publish(
                QueueAnnotationReplyEvent(message_annotation_id=annotation_reply.id),
                PublishFrom.APPLICATION_MANAGER
            )

            self._stream_output(
                queue_manager=queue_manager,
                text=annotation_reply.content,
                stream=app_generate_entity.stream,
                stopped_by=QueueStopEvent.StopBy.ANNOTATION_REPLY
            )
            return True

        return False

    def _stream_output(self, queue_manager: AppQueueManager,
                       text: str,
                       stream: bool,
                       stopped_by: QueueStopEvent.StopBy) -> None:
        """
        Direct output
        :param queue_manager: application queue manager
        :param text: text
        :param stream: stream
        :return:
        """
        if stream:
            index = 0
            for token in text:
                queue_manager.publish(
                    QueueTextChunkEvent(
                        text=token
                    ), PublishFrom.APPLICATION_MANAGER
                )
                index += 1
                time.sleep(0.01)
        else:
            queue_manager.publish(
                QueueTextChunkEvent(
                    text=text
                ), PublishFrom.APPLICATION_MANAGER
            )

        queue_manager.publish(
            QueueStopEvent(stopped_by=stopped_by),
            PublishFrom.APPLICATION_MANAGER
        )
