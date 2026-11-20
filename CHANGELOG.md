# Changelog

## [Unreleased]

- Control the grid on every paper and cell look, with existing paper defaults preserved.
- Honour explicit cell colours and local fonts; Use dashboard keeps the widget's Paper.
- Give AI sheets 15 extra seconds before using line art on screens.
- Show each tool rack nozzle's filament name and swatch, with hatching on mono panels.
- Align tool-rack filaments, keep dotted leaders and move ACTIVE to the status column.

## 0.1.2

- The compare page no longer shows up under Widgets › Admin pages; it's still linked from the Picture option.

## 0.1.1

- Toolpaths no longer lose parts of a print (a gun barrel, the top of a skull) to a tight bounding-box clip.
- Toolpaths, line art and AI engraving give the picture the same room on the glass.
- Unprinted layers dash along straight walls too, not only on curves.

## 0.1.0

- First release: Bambu Lab prints as blueprint sheets, with toolpath, line-art and AI engraving pictures.
- Preview line art keeps fine details with sharp contours and lighter, separated shadow strokes.
- Preview each method with a bundled T800 example; choose Live print for free live previews.
- Method thumbnails follow the cell's shape, paper and panel.
- Keep paid AI tasks through failures and restarts, and respect the server's AI spending ceilings.
- Store button colours in one file; saved Paper changes reset the override without preview side effects.
- A refresh button mapped to `print_blueprint_cycle` cycles the background white, blue, black.
- Spectra panels get the panel's own inks, so text no longer breaks up into dither.
- Bigger text, title and times; times follow the device locale's 12- or 24-hour clock.
- A title that fits takes one line; the cycle button no longer holds the frame on a print page.
- A kie.ai picture that times out is collected later instead of paid for again.
- The Picture option greys out Toolpaths for a printer Home Assistant gets no sliced files from, and AI engraving without a kie.ai key, and says why.
