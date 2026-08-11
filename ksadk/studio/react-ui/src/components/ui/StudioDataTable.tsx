import { useMemo, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import {
  createColumnHelper,
  tableFeatures,
  useTable,
  type RowData,
} from "@tanstack/react-table";
import { AlertCircle, ChevronLeft, ChevronRight, LoaderCircle } from "lucide-react";

const studioTableFeatures = tableFeatures({});

export interface StudioDataColumn<TData extends RowData> {
  id: string;
  header: ReactNode;
  cell: (row: TData) => ReactNode;
  width?: number | string;
  minWidth?: number | string;
  className?: string;
  headerClassName?: string;
}

export interface StudioDataTableEmptyState {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}

export interface StudioDataTablePagination {
  pageIndex: number;
  pageSize: number;
  total: number;
  hasNextPage: boolean;
  onPreviousPage: () => void;
  onNextPage: () => void;
}

export interface StudioDataTableProps<TData extends RowData> {
  columns: StudioDataColumn<TData>[];
  data: TData[];
  getRowId: (row: TData) => string;
  caption?: string;
  minWidth?: number | string;
  loading?: boolean;
  error?: string;
  onRetry?: () => void;
  empty?: StudioDataTableEmptyState;
  pagination?: StudioDataTablePagination;
  onRowActivate?: (row: TData) => void;
  rowAriaLabel?: (row: TData) => string;
}

function cssSize(value: number | string | undefined): string | undefined {
  return typeof value === "number" ? `${value}px` : value;
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("button, a, input, select, textarea, [role='button']"));
}

export function StudioDataTable<TData extends RowData>({
  columns,
  data,
  getRowId,
  caption,
  minWidth = 880,
  loading = false,
  error = "",
  onRetry,
  empty = { title: "没有数据" },
  pagination,
  onRowActivate,
  rowAriaLabel,
}: StudioDataTableProps<TData>) {
  const helper = useMemo(
    () => createColumnHelper<typeof studioTableFeatures, TData>(),
    [],
  );
  const tableColumns = useMemo(
    () => helper.columns(columns.map(column => helper.display({
      id: column.id,
      header: () => column.header,
      cell: context => column.cell(context.row.original),
    }))),
    [columns, helper],
  );
  const table = useTable({
    features: studioTableFeatures,
    columns: tableColumns,
    data,
    getRowId,
  });

  const activateFromKeyboard = (event: KeyboardEvent<HTMLTableRowElement>, row: TData) => {
    if (!onRowActivate || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    onRowActivate(row);
  };

  const pageStart = pagination && data.length
    ? pagination.pageIndex * pagination.pageSize + 1
    : 0;
  const pageEnd = pagination
    ? Math.min(pagination.pageIndex * pagination.pageSize + data.length, pagination.total)
    : 0;

  return (
    <div className="studio-data-table" aria-busy={loading || undefined}>
      <div className="studio-data-table-scroll data-scroll-region">
        {!loading && !error && (
          <table style={{ minWidth: cssSize(minWidth) }}>
            {caption && <caption className="sr-only">{caption}</caption>}
            <thead>
              {table.getHeaderGroups().map(headerGroup => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header, index) => {
                    const column = columns[index];
                    const style: CSSProperties = {
                      width: cssSize(column?.width),
                      minWidth: cssSize(column?.minWidth),
                    };
                    return (
                      <th key={header.id} className={column?.headerClassName} style={style}>
                        {header.isPlaceholder ? null : <table.FlexRender header={header} />}
                      </th>
                    );
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map(row => (
                <tr
                  key={row.id}
                  className={onRowActivate ? "is-interactive" : undefined}
                  tabIndex={onRowActivate ? 0 : undefined}
                  aria-label={rowAriaLabel?.(row.original)}
                  onKeyDown={event => activateFromKeyboard(event, row.original)}
                  onClick={event => {
                    if (onRowActivate && !isInteractiveTarget(event.target)) onRowActivate(row.original);
                  }}
                >
                  {row.getAllCells().map((cell, index) => (
                    <td key={cell.id} className={columns[index]?.className}>
                      <table.FlexRender cell={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {loading && (
          <div className="studio-data-table-state" role="status">
            <LoaderCircle className="animate-spin" size={20} />
            <strong>正在加载</strong>
            <span>正在同步最新数据…</span>
          </div>
        )}

        {!loading && error && (
          <div className="studio-data-table-state is-error" role="alert">
            <AlertCircle size={20} />
            <strong>加载失败</strong>
            <span>{error}</span>
            {onRetry && (
              <button className="button secondary small" type="button" onClick={onRetry}>
                重新加载
              </button>
            )}
          </div>
        )}

        {!loading && !error && data.length === 0 && (
          <div className="studio-data-table-state is-empty">
            {empty.icon && <span className="empty-icon">{empty.icon}</span>}
            <strong>{empty.title}</strong>
            {empty.description && <span>{empty.description}</span>}
            {empty.action}
          </div>
        )}
      </div>

      {pagination && !loading && !error && (
        <footer className="studio-data-table-pagination">
          <span>
            第 {pagination.pageIndex + 1} 页 · {pageStart}–{pageEnd} / {pagination.total} 条
          </span>
          <div>
            <button
              className="button tertiary small"
              type="button"
              disabled={pagination.pageIndex <= 0}
              onClick={pagination.onPreviousPage}
            >
              <ChevronLeft size={14} />上一页
            </button>
            <button
              className="button tertiary small"
              type="button"
              disabled={!pagination.hasNextPage}
              onClick={pagination.onNextPage}
            >
              下一页<ChevronRight size={14} />
            </button>
          </div>
        </footer>
      )}
    </div>
  );
}
