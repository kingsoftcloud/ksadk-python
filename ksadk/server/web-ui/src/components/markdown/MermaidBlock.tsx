import React, { useEffect, useId, useState } from 'react';
import mermaid from 'mermaid';

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'strict',
});

interface MermaidBlockProps {
  chart: string;
}

export const MermaidBlock: React.FC<MermaidBlockProps> = ({ chart }) => {
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<boolean>(false);
  const id = useId().replace(/:/g, '-');

  useEffect(() => {
    let isCancelled = false;
    const renderChart = async () => {
      try {
        const { svg: renderedSvg } = await mermaid.render(id, chart);
        if (!isCancelled) {
          setSvg(renderedSvg);
          setError(false);
        }
      } catch {
        if (!isCancelled) {
          setError(true);
        }
      }
    };
    renderChart();
    return () => { isCancelled = true; };
  }, [chart, id]);

  if (error) {
    return (
      <pre className="my-4 p-4 bg-slate-100 dark:bg-slate-800 rounded-lg overflow-x-auto text-sm text-red-500 border border-red-200 dark:border-red-800">
        {chart}
      </pre>
    );
  }

  return (
    <div
      className="my-4 flex justify-center bg-white dark:bg-slate-800 p-4 rounded-lg border border-slate-200 dark:border-slate-700 overflow-x-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
};