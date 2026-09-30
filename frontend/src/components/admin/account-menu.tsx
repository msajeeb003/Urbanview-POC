"use client";

/**
 * The signed-in staff member in the admin bar (not in the mock): one compact button (role chip +
 * name) so the wireframe's bar still fits at 1440 px with every tab, opening a small menu with
 * the full e-mail, the admin-only link (Users) and "Sign out". The menu is fixed-
 * positioned under the button because `.adminbar` scrolls horizontally (it would clip it).
 */
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { signOutAction } from "@/lib/admin/actions";
import type { Section } from "@/lib/admin/sections";

import type { FrameUser } from "./admin-frame";

export function AccountMenu({ user, links }: { user: FrameUser; links: Section[] }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<{ top: number; right: number } | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const label = user.name || user.email?.split("@")[0] || "service";

  useEffect(() => {
    if (!open) return;
    const rect = button.current?.getBoundingClientRect();
    if (rect) setPlace({ top: rect.bottom + 6, right: Math.max(8, window.innerWidth - rect.right) });
    const close = (e: Event) => {
      const target = e.target as Node;
      if (menu.current?.contains(target) || button.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  return (
    <>
      <button
        ref={button}
        type="button"
        className="abtn sm ghost admin-who"
        aria-haspopup="menu"
        aria-expanded={open}
        title={user.email ?? undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="st rev">{user.role}</span>
        <span className="admin-who-name">{label}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {open && place && (
        <div ref={menu} className="admin-menu" role="menu" style={{ top: place.top, right: place.right }}>
          <div className="admin-menu-head">
            <span className="mono">{user.openAccess ? "open access" : (user.email ?? "service token")}</span>
            <span className="admin-menu-role">
              {user.openAccess ? `Sign-in is switched off · ${user.role}` : `Signed in as ${user.role}`}
            </span>
          </div>
          {links.map((l) => (
            <button
              key={l.id}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                router.push(l.href);
              }}
            >
              {l.label}
            </button>
          ))}
          {!user.openAccess && (
            <form action={signOutAction}>
              <button type="submit" role="menuitem">
                Sign out
              </button>
            </form>
          )}
        </div>
      )}
    </>
  );
}
