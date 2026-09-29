import Link from "next/link";

/**
 * The review queue's two lists (the pilot scope's A2: extracted values and geometry drafts are
 * reviewed the same way): a switch under the card title with each list's pending count.
 */
export function ReviewTabs({
  active,
  valuesPending,
  geometryPending,
}: {
  active: "values" | "geometry";
  valuesPending: number | null;
  geometryPending: number | null;
}) {
  const count = (n: number | null) => (n == null ? null : <span className={n ? "rvtabn pend" : "rvtabn"}>{n} pending</span>);
  return (
    <nav className="rvtabs" aria-label="What to review">
      <Link href="/admin/review" className={active === "values" ? "rvtab on" : "rvtab"} aria-current={active === "values" ? "page" : undefined}>
        Extracted values {count(valuesPending)}
      </Link>
      <Link
        href="/admin/review/geometry"
        className={active === "geometry" ? "rvtab on" : "rvtab"}
        aria-current={active === "geometry" ? "page" : undefined}
      >
        Geometry {count(geometryPending)}
      </Link>
    </nav>
  );
}
