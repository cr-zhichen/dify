import type { ClipboardEvent } from 'react'
import {
  useCallback,
  useState,
} from 'react'
import produce from 'immer'
import { v4 as uuid4 } from 'uuid'
import { useTranslation } from 'react-i18next'
import type { TFile } from './types'
import { useFileStore } from './store'
import { fileUpload } from './utils'
import { useToastContext } from '@/app/components/base/toast'

type UseFileParams = {
  isPublicAPI?: boolean
  url?: string
}
export const useFile = ({
  isPublicAPI,
  url,
}: UseFileParams) => {
  const { t } = useTranslation()
  const { notify } = useToastContext()
  const fileStore = useFileStore()

  const handleAddOrUpdateFiles = useCallback((newFile: TFile) => {
    const {
      files,
      setFiles,
    } = fileStore.getState()

    const newFiles = produce(files, (draft) => {
      const index = draft.findIndex(file => file._id === newFile._id)

      if (index > -1)
        draft[index] = newFile
      else
        draft.push(newFile)
    })
    setFiles(newFiles)
  }, [fileStore])

  const handleRemoveFile = useCallback((fileId: string) => {
    const {
      files,
      setFiles,
    } = fileStore.getState()

    const newFiles = files.filter(file => file._id !== fileId)
    setFiles(newFiles)
  }, [fileStore])

  const handleLoadFileFromLink = useCallback((fileId: string, progress: number) => {
    const {
      files,
      setFiles,
    } = fileStore.getState()
    const newFiles = produce(files, (draft) => {
      const index = draft.findIndex(file => file._id === fileId)

      if (index > -1)
        draft[index]._progress = progress
    })
    setFiles(newFiles)
  }, [fileStore])

  const handleClearFiles = useCallback(() => {
    const {
      setFiles,
    } = fileStore.getState()
    setFiles([])
  }, [fileStore])

  const handleLocalFileUpload = useCallback((file: File) => {
    const reader = new FileReader()
    reader.addEventListener(
      'load',
      () => {
        const imageFile = {
          _id: uuid4(),
          file,
          _url: reader.result as string,
          _progress: 0,
        }
        handleAddOrUpdateFiles(imageFile)
        fileUpload({
          file: imageFile.file,
          onProgressCallback: (progress) => {
            handleAddOrUpdateFiles({ ...imageFile, _progress: progress })
          },
          onSuccessCallback: (res) => {
            handleAddOrUpdateFiles({ ...imageFile, _fileId: res.id, _progress: 100 })
          },
          onErrorCallback: () => {
            notify({ type: 'error', message: t('common.imageUploader.uploadFromComputerUploadError') })
            handleAddOrUpdateFiles({ ...imageFile, _progress: -1 })
          },
        }, isPublicAPI, url)
      },
      false,
    )
    reader.addEventListener(
      'error',
      () => {
        notify({ type: 'error', message: t('common.imageUploader.uploadFromComputerReadError') })
      },
      false,
    )
    reader.readAsDataURL(file)
  }, [notify, t, handleAddOrUpdateFiles, isPublicAPI, url])

  const handleClipboardPasteFile = useCallback((e: ClipboardEvent<HTMLTextAreaElement>) => {
    const file = e.clipboardData?.files[0]
    if (file) {
      e.preventDefault()
      handleLocalFileUpload(file)
    }
  }, [handleLocalFileUpload])

  const [isDragActive, setIsDragActive] = useState(false)
  const handleDragFileEnter = useCallback((e: React.DragEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragActive(true)
  }, [])

  const handleDragFileOver = useCallback((e: React.DragEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])

  const handleDragFileLeave = useCallback((e: React.DragEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragActive(false)
  }, [])

  const handleDropFile = useCallback((e: React.DragEvent<HTMLElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragActive(false)

    const file = e.dataTransfer.files[0]

    if (file)
      handleLocalFileUpload(file)
  }, [handleLocalFileUpload])

  return {
    handleAddOrUpdateFiles,
    handleRemoveFile,
    handleLoadFileFromLink,
    handleClearFiles,
    handleLocalFileUpload,
    handleClipboardPasteFile,
    isDragActive,
    handleDragFileEnter,
    handleDragFileOver,
    handleDragFileLeave,
    handleDropFile,
  }
}
