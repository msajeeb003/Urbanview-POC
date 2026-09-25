"use client";

/**
 * AI assistant shell (wireframe `.aifab` + `.aipanel`, 380×544 anchored right 412 / bottom 20).
 * UI only: the assistant is not built in the pilot. Sending a question or picking a suggestion
 * records `ai_interest` (the demand signal the pilot measures) and says so; nothing is answered.
 * A panel CTA ("Ask about this document") opens it with its question typed in (`openAiWith`).
 */
import { useState } from "react";

import { useTrack } from "@/lib/analytics/react";
import { useT, type StringKey } from "@/lib/i18n";
import { useShell } from "@/lib/store";

import { IconChatAvatar, IconChatFab, IconSend } from "../ui/icons";

const FREE_TURNS = 3;
// the mock's chips and greeting; "ai.notYet" is provisional copy (the pilot records interest instead of answering)
const SUGGESTIONS: StringKey[] = ["ai.chip.build", "ai.chip.far", "ai.chip.smaller", "ai.chip.worth"];

export function AiFab() {
  const open = useShell((s) => s.aiOpen);
  const setOpen = useShell((s) => s.setAiOpen);
  const t = useT();
  return (
    <button
      type="button"
      className="aifab"
      id="aifab"
      title={t("ai.title")}
      aria-label={t("ai.title")}
      style={open ? { display: "none" } : undefined}
      onClick={() => setOpen(true)}
    >
      <IconChatFab />
      <span className="badge2" id="aibadge" style={{ background: "var(--brand)", color: "#fff" }}>
        {t("ai.free", { n: FREE_TURNS })}
      </span>
    </button>
  );
}

export function AiPanel() {
  const open = useShell((s) => s.aiOpen);
  const setOpen = useShell((s) => s.setAiOpen);
  const showToast = useShell((s) => s.showToast);
  const track = useTrack();
  const draft = useShell((s) => s.aiDraft);
  const t = useT();

  const interest = (trigger: "chip" | "input") => {
    track("ai_interest", { trigger });
    showToast(t("ai.notYet"));
  };

  return (
    <div className={open ? "aipanel on" : "aipanel"} id="aipanel" role="dialog" aria-label="UrbanView AI" aria-hidden={!open}>
      {open && (
        <>
          <div className="aihead">
            <div className="av">
              <IconChatAvatar />
            </div>
            <div className="t">
              <h4>UrbanView AI</h4>
              <p>{t("ai.tagline")}</p>
            </div>
            <button type="button" aria-label={t("ai.close")} onClick={() => setOpen(false)}>
              ✕
            </button>
          </div>
          <div className="aiquota">
            <span>{t("ai.quota", { n: FREE_TURNS })}</span>
            <span className="dots">
              {Array.from({ length: FREE_TURNS }, (_, i) => (
                <span key={i} className="qd" />
              ))}
            </span>
          </div>
          <div className="aibody" id="aibody">
            <div className="msg bot">
              <div className="b">{t("ai.greeting")}</div>
            </div>
          </div>
          <div className="aichips" id="aichips">
            {SUGGESTIONS.map((key) => (
              <button key={key} type="button" className="aichip" onClick={() => interest("chip")}>
                {t(key)}
              </button>
            ))}
          </div>
          <AiInput key={draft?.id ?? 0} initial={draft?.text ?? ""} onSend={() => interest("input")} />
        </>
      )}
    </div>
  );
}

/** The input row; a new draft (`key`) starts it with the panel's question, focused. */
function AiInput({ initial, onSend }: { initial: string; onSend: () => void }) {
  const [text, setText] = useState(initial);
  const t = useT();
  return (
    <form
      className="aifoot"
      onSubmit={(e) => {
        e.preventDefault();
        if (!text.trim()) return;
        setText("");
        onSend();
      }}
    >
      <input
        id="aiinput"
        placeholder={t("ai.placeholder")}
        aria-label={t("ai.inputLabel")}
        value={text}
        autoFocus={initial !== ""}
        onChange={(e) => setText(e.target.value)}
      />
      <button type="submit" className="send" aria-label={t("ai.send")}>
        <IconSend />
      </button>
    </form>
  );
}
