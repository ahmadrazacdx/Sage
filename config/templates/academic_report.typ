// ============================================================
//  Sage Academic Report Template
//  Typst 0.11+ compatible
//  Professional layout: cover page, TOC, numbered sections,
//  styled code blocks, footnote citations, page footer.
// ============================================================

// ── Document parameters (injected by export_pdf) ───────────
#let report-title    = sys.inputs.at("title",    default: "Research Report")
#let report-subtitle = sys.inputs.at("subtitle", default: "")
#let report-author   = sys.inputs.at("author",   default: "Sage Research Agent")
#let report-date     = sys.inputs.at("date",     default: "")
#let report-inst     = sys.inputs.at("inst",     default: "Thal University Bhakkar")
#let report-body     = sys.inputs.at("body",     default: "")

// ── Colour palette ─────────────────────────────────────────
#let sage-dark   = rgb("#0f2744")   // deep navy  – headers, cover bg
#let sage-mid    = rgb("#1a4a7a")   // mid blue   – rule lines
#let sage-accent = rgb("#2eaadc")   // teal       – decorative
#let sage-light  = rgb("#eaf4fb")   // pale teal  – shaded boxes
#let sage-text   = rgb("#1c2b3a")   // near-black – body text
#let sage-muted  = rgb("#6b7f91")   // slate      – captions / footers

// ── Page geometry ──────────────────────────────────────────
#set page(
  paper: "a4",
  margin: (top: 2.8cm, bottom: 2.8cm, left: 3cm, right: 2.5cm),
  footer: context {
    let pg = counter(page).get().first()
    let total = counter(page).final().first()
    if pg > 1 [
      #set text(size: 8.5pt, fill: sage-muted)
      #grid(
        columns: (1fr, auto, 1fr),
        align(left, text(report-title, style: "italic")),
        align(center)[#pg / #total],
        align(right, text("Sage · " + report-inst)),
      )
      #line(length: 100%, stroke: 0.4pt + sage-muted)
    ]
  },
)

// ── Typography ─────────────────────────────────────────────
#set text(
  font: ("New Computer Modern", "Linux Libertine", "Georgia"),
  size: 11pt,
  fill: sage-text,
  lang: "en",
)
#set par(justify: true, leading: 0.75em, first-line-indent: 0em)

// ── Headings ───────────────────────────────────────────────
#set heading(numbering: "1.1.")

#show heading.where(level: 1): it => {
  v(1.4em)
  block[
    #line(length: 100%, stroke: 1.6pt + sage-mid)
    #v(0.25em)
    #text(
      size: 14pt, weight: "bold", fill: sage-dark,
      upper(it.body),
    )
    #v(0.2em)
    #line(length: 100%, stroke: 0.4pt + sage-accent)
  ]
  v(0.5em)
}

#show heading.where(level: 2): it => {
  v(1em)
  text(size: 12pt, weight: "bold", fill: sage-mid, it.body)
  v(0.4em)
}

#show heading.where(level: 3): it => {
  v(0.8em)
  text(size: 11pt, weight: "bold", style: "italic", fill: sage-text, it.body)
  v(0.3em)
}

// ── Code blocks ────────────────────────────────────────────
#show raw.where(block: true): it => {
  block(
    width: 100%,
    fill: sage-light,
    stroke: (left: 3pt + sage-accent, rest: 0.5pt + sage-muted.lighten(40%)),
    radius: 4pt,
    inset: (x: 12pt, y: 10pt),
    text(font: ("JetBrains Mono", "Fira Code", "Courier New"), size: 9.5pt, it),
  )
}

#show raw.where(block: false): it => {
  box(
    fill: sage-light,
    inset: (x: 4pt, y: 2pt),
    radius: 3pt,
    text(font: ("JetBrains Mono", "Fira Code", "Courier New"), size: 9.5pt, it),
  )
}

// ── Block quote / callout ──────────────────────────────────
#show quote: it => {
  block(
    width: 100%,
    fill: sage-light,
    stroke: (left: 3pt + sage-accent),
    inset: (x: 14pt, y: 8pt),
    radius: (right: 4pt),
    text(style: "italic", fill: sage-muted, it.body),
  )
}

// ── Figure captions ────────────────────────────────────────
#show figure.caption: it => {
  text(size: 9pt, fill: sage-muted, style: "italic")[
    #it.supplement #context it.counter.display(it.numbering): #it.body
  ]
}

// ── Links ──────────────────────────────────────────────────
#show link: it => {
  text(fill: sage-accent, it)
}

// ════════════════════════════════════════════════════════════
//  COVER PAGE
// ════════════════════════════════════════════════════════════
#page(
  margin: 0pt,
  footer: none,
)[
  // Dark navy top band
  #block(
    width: 100%,
    fill: sage-dark,
  )[
    #v(4.5cm)
    #pad(x: 3cm)[
      // Decorative accent line
      #block(height: 4pt, width: 6cm, fill: sage-accent)
      #v(0.6em)
      #text(
        size: 26pt, weight: "bold", fill: white,
        hyphenate: false,
        report-title,
      )
      #if report-subtitle != "" {
        v(0.4em)
        text(size: 14pt, fill: sage-accent, style: "italic", report-subtitle)
      }
    ]
    #v(1.2em)
  ]

  // White lower portion
  #block(
    width: 100%,
    fill: white,
  )[
    #pad(x: 3cm, y: 2cm)[
      #grid(
        columns: (1fr, auto),
        gutter: 1.5em,
        // Left: meta
        [
          #v(0.6em)
          #text(size: 10.5pt, fill: sage-muted)[*Prepared by*]
          #v(0.15em)
          #text(size: 13pt, weight: "bold", fill: sage-dark, report-author)

          #v(0.9em)
          #text(size: 10.5pt, fill: sage-muted)[*Institution*]
          #v(0.15em)
          #text(size: 12pt, fill: sage-text, report-inst)

          #v(0.9em)
          #text(size: 10.5pt, fill: sage-muted)[*Date*]
          #v(0.15em)
          #text(
            size: 12pt, fill: sage-text,
            if report-date != "" { report-date } else { datetime.today().display("[month repr:long] [day], [year]") }
          )
        ],
        // Right: Sage badge
        [
          #block(
            width: 3.5cm, height: 3.5cm,
            fill: sage-light,
            radius: 50%,
            stroke: 2pt + sage-accent,
          )[
            #align(center + horizon)[
              #text(size: 9.5pt, weight: "bold", fill: sage-dark)[SAGE]
            ]
          ]
        ],
      )

      #v(2em)
      #line(length: 100%, stroke: 0.5pt + sage-muted.lighten(40%))
      #v(0.5em)
      #text(size: 8.5pt, fill: sage-muted, style: "italic")[
        This report synthesises multiple sources and is intended for educational review.
      ]
    ]
  ]
]

// ════════════════════════════════════════════════════════════
//  TABLE OF CONTENTS  (auto, page 2)
// ════════════════════════════════════════════════════════════
#counter(page).update(1)

#outline(
  title: [
    #text(size: 16pt, weight: "bold", fill: sage-dark)[Contents]
    #v(0.3em)
    #line(length: 100%, stroke: 1.2pt + sage-mid)
    #v(0.6em)
  ],
  depth: 3,
  indent: auto,
)

#pagebreak()

// ════════════════════════════════════════════════════════════
//  BODY  (injected markdown converted to Typst)
// ════════════════════════════════════════════════════════════

#report-body
