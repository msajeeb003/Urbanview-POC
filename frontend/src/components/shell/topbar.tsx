"use client";

/**
 * Topbar (wireframe `.topbar`, 62 px, #241B12): combination-mark logo, centred search, nav
 * buttons "Map" / "Admin", the language switch (not in the mock: EN ⇄ ME, shows the language it
 * switches to) and the municipality pill that re-centres the map.
 */
import { forwardRef } from "react";

import { useMunicipality } from "@/lib/api/hooks";
import { useLang, useT } from "@/lib/i18n";
import { useShell } from "@/lib/store";

import { IconAdmin, IconMap } from "../ui/icons";

import { SearchBox } from "./search-box";

export const Topbar = forwardRef<HTMLInputElement>(function Topbar(_, searchRef) {
  const view = useShell((s) => s.view);
  const setView = useShell((s) => s.setView);
  const { data: profile } = useMunicipality();
  const { lang, setLang } = useLang();
  const t = useT();
  const name = profile?.name ?? "";

  return (
    <div className="topbar">
      <div className="brand">
        {/* eslint-disable-next-line @next/next/no-img-element -- SVG logo, sized by the stylesheet */}
        <img className="logo" src="/brand/UrbanView_logo.svg" alt="UrbanView" />
      </div>
      <SearchBox ref={searchRef} />
      <div className="topnav">
        <button
          type="button"
          id="navMap"
          className={view === "map" ? "active" : undefined}
          aria-pressed={view === "map"}
          onClick={() => setView("map")}
        >
          <IconMap />
          <span className="navlbl">{t("nav.map")}</span>
        </button>
        <button
          type="button"
          id="navAdmin"
          className={view === "admin" ? "active" : undefined}
          aria-pressed={view === "admin"}
          onClick={() => setView("admin")}
        >
          <IconAdmin />
          <span className="navlbl">{t("nav.admin")}</span>
        </button>
        <button
          type="button"
          className="langbtn"
          title={t("lang.switch")}
          aria-label={t("lang.switch")}
          onClick={() => setLang(lang === "en" ? "me" : "en")}
        >
          {t("lang.code")}
        </button>
        <button
          type="button"
          className="pill"
          id="homePill"
          title={name ? t("nav.returnTo", { name }) : t("nav.returnToCity")}
          onClick={() => {
            const s = useShell.getState();
            s.setView("map");
            s.map?.reset();
            if (name) s.showToast(t("toast.centred", { name }));
          }}
        >
          {name ? t("nav.pill", { name: name.toUpperCase() }) : t("nav.pillNoName")}
        </button>
      </div>
    </div>
  );
});
