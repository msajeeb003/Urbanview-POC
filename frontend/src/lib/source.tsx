"use client";

/**
 * One click from a value to the page of the document it cites: every source chip, row source
 * icon and document-list icon calls `useOpenSource()`, which opens the source viewer
 * (`components/source/source-viewer.tsx`) in a wide modal over the map. The viewer asks for the
 * signed link, renders the page with PDF.js, highlights the cited value and emits
 * `source_reference_opened { document_id, page, value_id }`.
 */
import { useCallback } from "react";

import { SourceViewer } from "@/components/source/source-viewer";
import { useShell } from "@/lib/store";

export type SourceTarget = { documentId: number; page: number } | { valueId: number };

export const SOURCE_LABEL = "Source document";

export function useOpenSource() {
  const openModal = useShell((s) => s.openModal);
  return useCallback(
    (target: SourceTarget) =>
      openModal({ label: SOURCE_LABEL, wide: true, className: "srcmodal", content: <SourceViewer target={target} /> }),
    [openModal],
  );
}
