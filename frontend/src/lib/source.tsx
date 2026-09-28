"use client";

/**
 * One click from a value to the page of the document it cites: every source chip, row source
 * icon and document-list icon calls `useOpenSource()`, which opens the source viewer
 * (`components/source/source-viewer.tsx`) in a wide modal over the map. The viewer asks for the
 * signed link, renders the page with PDF.js, highlights the cited value and emits
 * `source_reference_opened { document_id, page, value_id }`. The opener passes what it already
 * knows (`hint`: document name, page, field label, note, registry link), so the viewer names the
 * document and page while loading and when the page cannot be shown.
 */
import { useCallback } from "react";

import { SourceViewer } from "@/components/source/source-viewer";
import type { SourceHint } from "@/lib/source-text";
import { useShell } from "@/lib/store";

export type SourceTarget = ({ documentId: number; page: number } | { valueId: number }) & { hint?: SourceHint };

export const SOURCE_LABEL = "Source document";

export function useOpenSource() {
  const openModal = useShell((s) => s.openModal);
  return useCallback(
    (target: SourceTarget) =>
      openModal({ label: SOURCE_LABEL, wide: true, className: "srcmodal", content: <SourceViewer target={target} /> }),
    [openModal],
  );
}
