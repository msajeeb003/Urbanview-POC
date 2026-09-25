"use client";

/** The single modal slot and the bottom-centre toast of the shell (wireframe `#overlay`, `#toast`). */
import { useEffect } from "react";

import { useShell } from "@/lib/store";

import { Modal } from "../ui/modal";

export const TOAST_MS = 2600;

export function ModalHost() {
  const modal = useShell((s) => s.modal);
  const closeModal = useShell((s) => s.closeModal);
  return (
    <Modal open={!!modal} onOpenChange={(open) => !open && closeModal()} wide={modal?.wide} className={modal?.className} label={modal?.label ?? "Dialog"}>
      {modal?.content}
    </Modal>
  );
}

/** Dark toast, `✦` + message, slides up and hides after 2.6 s (a new toast restarts the timer). */
export function ToastHost() {
  const toast = useShell((s) => s.toast);
  const hideToast = useShell((s) => s.hideToast);
  const id = toast?.visible ? toast.id : null;

  useEffect(() => {
    if (id === null) return;
    const t = setTimeout(() => hideToast(id), TOAST_MS);
    return () => clearTimeout(t);
  }, [id, hideToast]);

  return (
    <div className={toast?.visible ? "toast on" : "toast"} id="toast" role="status" aria-live="polite">
      {toast && (
        <>
          <span className="ti">✦</span> {toast.message}
        </>
      )}
    </div>
  );
}
