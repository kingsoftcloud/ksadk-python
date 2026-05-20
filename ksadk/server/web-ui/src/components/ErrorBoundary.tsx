import React from 'react';

type ErrorBoundaryProps = {
  children: React.ReactNode;
};

type ErrorBoundaryState = {
  hasError: boolean;
  error: Error | null;
};

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('UI 渲染错误:', error, info);
  }

  handleReload = () => {
    this.setState({ hasError: false, error: null });
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-screen w-screen items-center justify-center bg-slate-50 dark:bg-slate-900">
          <div className="max-w-md rounded-2xl border border-red-200 bg-white p-8 text-center shadow-lg dark:border-red-900 dark:bg-slate-950">
            <div className="mb-4 text-4xl">&#9888;</div>
            <h1 className="mb-2 text-xl font-semibold text-red-600 dark:text-red-400">
              页面渲染出错
            </h1>
            <p className="mb-6 text-sm text-slate-600 dark:text-slate-400">
              {this.state.error?.message || '发生了未知错误，请刷新页面重试。'}
            </p>
            <button
              onClick={this.handleReload}
              className="rounded-lg bg-blue-600 px-6 py-2.5 text-sm font-medium text-white transition hover:bg-blue-700"
            >
              刷新页面
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
