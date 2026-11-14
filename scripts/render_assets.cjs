// Render the bot pictures in assets/ from their SVG sources with resvg.
// The renderer is not a project dependency; install it anywhere first:
//   npm install --prefix /some/dir @resvg/resvg-js@2.6.2
//   NODE_PATH=/some/dir/node_modules node scripts/render_assets.cjs
// Writes assets/avatar.png (640x640) and assets/welcome.png (640x360).
// The welcome text uses Segoe UI from the system fonts (Windows), falling
// back to the system sans-serif elsewhere.
const fs = require("fs");
const path = require("path");
const { Resvg } = require("@resvg/resvg-js");

const assets = path.join(__dirname, "..", "assets");

for (const [name, width] of [["avatar", 640], ["welcome", 640]]) {
  const svg = fs.readFileSync(path.join(assets, `${name}.svg`), "utf8");
  const png = new Resvg(svg, {
    fitTo: { mode: "width", value: width },
    font: { loadSystemFonts: true, defaultFontFamily: "Segoe UI" },
  }).render();
  fs.writeFileSync(path.join(assets, `${name}.png`), png.asPng());
  console.log(`wrote assets/${name}.png (${png.width}x${png.height})`);
}
