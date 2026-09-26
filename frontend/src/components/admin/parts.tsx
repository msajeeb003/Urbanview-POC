/**
 * The admin console's building blocks, in the wireframe's markup (`renderAdmin` and the tab
 * templates in docs/wireframe/wireframe.js): the stylesheet already styles every class, so
 * nothing here adds colours or sizes.
 *
 * - `AdminCard`: `.card > .cardhd (h3 + .sub, right-side action)` + body;
 * - `DataTable`: `.tbl`, numbers and references in `.mono` cells;
 * - `StatusChip`: `.st.ok | .st.pend | .st.rev` (dot + uppercase mono word);
 * - `AdminButton`: `.abtn`, `.ghost`, `.sm`;
 * - `StatCard`: `.astat > .sl + .sv (+ small) + .sd (.up | .warn)`.
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ChipTone = "ok" | "pend" | "rev";

export function StatusChip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`st ${tone}`}>{children}</span>;
}

export function AdminButton({
  variant,
  size,
  className,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "ghost"; size?: "sm" }) {
  const classes = ["abtn", variant, size, className].filter(Boolean).join(" ");
  return <button type={type} className={classes} {...props} />;
}

export function AdminCard({
  title,
  sub,
  action,
  children,
}: {
  title: ReactNode;
  sub?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="card">
      <div className="cardhd">
        <div>
          <h3>{title}</h3>
          {sub != null && <div className="sub">{sub}</div>}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

export interface Column<T> {
  key: string;
  label: ReactNode;
  /** Numbers, references, codes, times: `.mono`. */
  mono?: boolean;
  render: (row: T) => ReactNode;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  empty = "Nothing here yet.",
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string | number;
  empty?: ReactNode;
}) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key}>{c.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td colSpan={columns.length} className="tbl-empty">
              {empty}
            </td>
          </tr>
        ) : (
          rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
              {columns.map((c) => (
                <td key={c.key} className={c.mono ? "mono" : undefined}>
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

export function StatCard({
  label,
  value,
  small,
  note,
  noteTone,
  warn,
}: {
  label: string;
  value: ReactNode;
  small?: ReactNode;
  note?: ReactNode;
  noteTone?: "up" | "warn";
  warn?: boolean;
}) {
  return (
    <div className="astat">
      <div className="sl">{label}</div>
      <div className={warn ? "sv warn" : "sv"}>
        {value}
        {small != null && <small> {small}</small>}
      </div>
      {note != null && <div className={noteTone ? `sd ${noteTone}` : "sd"}>{note}</div>}
    </div>
  );
}

/** A section the role cannot open: plain, no error colours (the spec's "no red state"). */
export function NoAccess({ home, roleLabel }: { home: string | null; roleLabel?: string }) {
  return (
    <AdminCard
      title="You don't have access to this section"
      sub={roleLabel ? `Your role (${roleLabel}) does not include it.` : undefined}
    >
      <div className="admin-note">
        Ask an administrator if you need it.
        {home && (
          <a className="abtn sm ghost" href={home}>
            Go to your sections
          </a>
        )}
      </div>
    </AdminCard>
  );
}

/** The API could not be reached: said plainly, with a retry (the page reloads its data). */
export function AdminUnavailable({ what }: { what: string }) {
  return (
    <AdminCard title={what} sub="The data service did not answer.">
      <div className="admin-note">
        Try again in a moment.
        <a className="abtn sm ghost" href="">
          Retry
        </a>
      </div>
    </AdminCard>
  );
}
