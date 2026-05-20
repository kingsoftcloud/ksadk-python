export type TransportEvent = {
  eventName: string;
  data: unknown;
};

export type TransportCallbacks = {
  onEvent: (event: TransportEvent) => void;
  onError: (error: Error) => void;
  onComplete: () => void;
};

export interface RuntimeTransport {
  connect(
    options: {
      action: string;
      method?: 'POST' | 'GET';
      body?: Record<string, unknown>;
      params?: Record<string, string>;
      signal?: AbortSignal;
    },
    callbacks: TransportCallbacks,
  ): Promise<() => void>;
  readonly protocol: string;
}
