import { postFormAction } from './client.js';

type UploadFileResponse = {
  FileData: {
    fileUri: string;
    displayName: string;
    mimeType: string;
  };
};

export async function uploadFile(
  formData: FormData,
  options?: { signal?: AbortSignal },
): Promise<UploadFileResponse> {
  return postFormAction<UploadFileResponse>('UploadFile', formData, options);
}
