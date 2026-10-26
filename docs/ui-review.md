# PaperTrail UI review

Reviewed on 16 October 2026, in the `codex/astra-ui` worktree. Scope: frontend presentation and interface text only. Demo launched with `LIVE=0 python3 -m papertrail.web --demo`; no credentials, external model calls or deployments. The existing `.venv` symlink is broken, so system Python was used.

## Judge's first impression — before

The product's basic purpose is understandable in ten seconds, but the page feels like an internal tool. The opening asks for an upload while demo mode removes the upload form. The next heading introduces recorded models rather than a useful task. Four equally prominent green buttons dominate the page; there is no single starting point or illustration of the output. A judge must commit to a sample before seeing why the product helps.

Captured the baseline in headless Chrome at 1280 × 900 and 390 × 900, with full-page screenshots: [desktop](ui-review/before-1280.png), [mobile](ui-review/before-390.png). Original HTML was restored temporarily from this branch's HEAD for baseline capture, then the working frontend was restored. No backend files were changed.

## Prioritized findings

| Priority | Finding | Impact / resolution |
| --- | --- | --- |
| P1 | No clear primary demo action; upload-first introduction conflicts with demo behavior. | Added a first-screen “Try an example letter” action that opens a real recorded sample, plus audience, countries and output description. |
| P1 | “Every item is checked” overstates recorded and incomplete verification. | Explicitly identify recorded answers and source checks, unavailable uploads/live checks, and fixed historical sample dates. Explain each verification label. |
| P1 | Raw request/draft errors and verification reasons can expose internal language, including `judge verdict`. | Recoverable interface messages and a human-readable fallback for technical verification reasons. Checklist stays available if only the draft fails. |
| P1 | Two-column sample layout stays narrow on phones, creating tall, difficult-to-scan text blocks. | Single-column mobile cards with thumbnails, legible titles and receipt dates. Desktop uses four neutral cards. |
| P1 | Loading disables only the selected button; another sample can start a competing result. | A shared in-flight guard prevents concurrent submissions. Explicit waiting/progress/done/unavailable labels, request timeout and polling deadline. |
| P2 | Long evidence and letter quotations overwhelm the actionable output. | Keep requirements, dates, status and official source links visible. Put supporting quotations/reasons in keyboard-operable disclosures. |
| P2 | Uniform green surfaces and weak spacing hierarchy make the page look unfinished. | Restrained off-white/forest palette, larger headline, consistent borders, whitespace and an illustrative outcome card. System fonts; no external assets or CDN. |
| P2 | Case number is the result heading; empty cases provide no direction. | “Your action checklist” heading, case metadata below, meaningful empty state and “Dates & deadlines” terminology. |
| P2 | Accessibility relies on default focus behavior; copy buttons are small; statuses need explanation. | Explicit visible focus outlines, skip link, status focus on submission, 44px copy/disclosure targets, textual status legend and distinct badge colors. |
| P2 | Calendar and draft copy can suggest automatic action. | Explain that users review/send drafts themselves and import downloaded calendar files. |

## Visual and accessibility assessment

Before: no horizontal overflow at either tested width, but mobile cards are disproportionately tall. Basic text contrast is reasonable; the larger problems are hierarchy, density and discoverability. Existing labels and the polite status live region were useful and retained.

After: the primary action fits within the first mobile screen; the preview is explicitly illustrative. Color is supplemented by text for every verification state. Keyboard access includes navigation, sample cards, source links, disclosures, copy controls and case history. The forest/white buttons and dark body text use high-contrast pairs. This is a focused browser/keyboard review, not a complete assistive-technology certification.

## Validation and artifacts

Headless Chrome (Playwright, installed Chrome headless shell) exercised the local offline demo. Direct ad-hoc CDP navigation stalled in this environment; the Playwright launch worked normally, without request interception or external services.

- [After: desktop](ui-review/after-1280.png) and [after: mobile](ui-review/after-390.png).
- [Result: desktop](ui-review/after-result-1280.png) and [result: mobile](ui-review/after-result-390.png).
- [Recoverable error: mobile](ui-review/after-error-390.png), using a browser-intercepted simulated failed job; fixtures and backend remain unchanged.
- Browser checks cover the hero action, all four samples, evidence disclosures, copying/fallback, `.ics` download, case timeline, loading/error recovery and retained non-demo upload controls. Non-demo configuration was simulated only to inspect the form; no live processing was performed.
- Both viewport widths match document scroll width. No JavaScript exceptions or console errors on the checked scenarios.

## Remaining product limitations

This is still an offline demo with recorded responses. Upload processing and current government-site verification cannot be demonstrated in this mode. Checklist content remains in the original letter's language; the English translation belongs to the reply draft, not the complete checklist. This is now disclosed alongside results. Sample dates can appear overdue. Case persistence follows the existing server behavior; no account, reminder delivery service or automatic reply sending was introduced. Backend functionality, API payloads, fixtures and logic tests are unchanged.

Final test command: `LIVE=0 python3 -m pytest -q` (system `pytest`; worktree `.venv/bin/pytest` unavailable).

```text
..................................................................... [ 38%]
.................................................... [ 67%]
.......................................... [ 91%]
...............                                                      [100%]
178 passed, 57 subtests passed in 15.39s
```

## v2 design direction

- **Concept — “Bureaucracy, tamed” («Бюрократия, приручённая»)**: Instead of a generic SaaS hero ("headline left + abstract card right" and a 1-2-3 stepper), the first screen stages an authentic Polish Voivodeship Office migration letter (`WSC-II-P.6151.48213.2026`) with highlighted legal/document/deadline clauses and three tactile callout annotations pinned directly over its margin (*Deadline stamp*, *What to bring*, *What to reply*). Judges see the transformation from intimidating administrative text into concrete actions in seconds. The generated checklist is styled as a fillable official form (бланк) with `☐` check-boxes, dashed form rules, and rubber-stamp badges.
- **Palette**: Crisp white paper (`#ffffff`) over a cool archival dossier desk (`#edf0f5`) with subtle ledger grid lines; deep **Ink Blue** (`#143a75`) as the primary brand and action color; **Stamp Vermilion Red** (`#bf311e` on `#fdf1ef`) for rotated deadline date stamps (`DUE 2026-10-12`); and official **Verification Green** (`#16653e` on `#eaf6f0`) reserved strictly for the `VERIFIED` rubber stamp rather than brand chrome. No cream backgrounds or forest-green brand styling.
- **Typography**: Expressive high-contrast serif (`Fraunces` / `Newsreader` / `EB Garamond` / `Charter`) for editorial headings and the letter specimen; crisp monospace (`IBM Plex Mono` / `JetBrains Mono` / `SF Mono` / `PT Mono`) for case numbers, statutory references (`art. 64 § 2 k.p.a.`), dates, and rubber stamps; clean grotesk (`Inter` / `system-ui`) for readable explanations and form controls.
- **Why**: The v1 cream-and-forest layout mirrored neighboring hackathon and business templates. Grounding PaperTrail in the visual language of migration dossiers, official forms, ink annotations, and date stamps gives the project its own unmistakable identity while keeping WCAG AA contrast, keyboard focus, honest offline-demo disclosures, and zero horizontal overflow at both 1280 px and 390 px.
- **v2 screenshots**: [v2 first screen (1280 px)](ui-review/v2-1280.png), [v2 first screen (390 px)](ui-review/v2-390.png), [v2 sample result (1280 px)](ui-review/v2-result-1280.png), [v2 sample result (390 px)](ui-review/v2-result-390.png).

