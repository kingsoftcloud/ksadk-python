export type AgentInputPart =
  | {
      type: 'input_text';
      text: string;
    }
  | {
      type: 'input_file';
      fileData: {
        fileUri: string;
        displayName: string;
        mimeType: string;
      };
    };