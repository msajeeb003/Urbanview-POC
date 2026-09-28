/**
 * One legal page (`/legal/<page>`, no mock screen): the order page's layout (topbar with the logo,
 * a card in the modal's style) with the text of `lib/legal.ts`, the draft notice first, links to
 * the other pages and back to the map. A server component: plain text, no state.
 */
import { LEGAL_DRAFT_NOTE, LEGAL_PAGES, LEGAL_SLUGS, legalPath, type LegalPage } from "@/lib/legal";

export function LegalPageView({ page }: { page: LegalPage }) {
  return (
    <div className="app">
      <header className="topbar">
        {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
        <a className="brand" href="/" aria-label="UrbanView — back to the map">
          {/* eslint-disable-next-line @next/next/no-img-element -- SVG logo, sized by the stylesheet */}
          <img className="logo" src="/brand/UrbanView_logo.svg" alt="UrbanView" />
        </a>
      </header>
      <main className="orderpage">
        <article className="ordercard" aria-labelledby="legal-title">
          <div className="mhead">
            <div className="mt">
              <div className="meyebrow">UrbanView · legal</div>
              <h2 id="legal-title">{page.title}</h2>
              <p>{page.lead}</p>
            </div>
          </div>
          <div className="mbody legaltext">
            <p className="legalnote">{LEGAL_DRAFT_NOTE}</p>
            {page.sections.map((section) => (
              <section key={section.heading}>
                <h3>{section.heading}</h3>
                {section.items && (
                  <ul>
                    {section.items.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
                {section.paragraphs?.map((text) => (
                  <p key={text}>{text}</p>
                ))}
              </section>
            ))}
          </div>
          <div className="mfoot">
            <nav className="legalnav legaltext" aria-label="Legal pages">
              {LEGAL_SLUGS.filter((slug) => slug !== page.slug).map((slug) => (
                <a key={slug} href={legalPath(slug)}>
                  {LEGAL_PAGES[slug].title}
                </a>
              ))}
            </nav>
            {/* a full page load: the map starts fresh, as from any link */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            <a className="cta ghost" href="/" style={{ width: "auto", padding: "0 18px", marginLeft: "auto" }}>
              ← Back to the map
            </a>
          </div>
        </article>
      </main>
    </div>
  );
}
