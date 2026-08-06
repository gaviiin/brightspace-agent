import { copyFileSync, existsSync, renameSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const root = dirname(fileURLToPath(import.meta.url));
const distDir = resolve(root, "dist");

// MV3 needs manifest.json at the dist root, and Vite's default multi-page
// HTML output mirrors the source path (dist/src/popup/popup.html) instead
// of the flat layout the manifest expects (default_popup: "popup.html") —
// this plugin copies the manifest in and flattens the popup output.
function extensionAssets(): Plugin {
  return {
    name: "extension-assets",
    closeBundle() {
      copyFileSync(resolve(root, "manifest.json"), resolve(distDir, "manifest.json"));

      const nestedPopup = resolve(distDir, "src/popup/popup.html");
      if (existsSync(nestedPopup)) {
        renameSync(nestedPopup, resolve(distDir, "popup.html"));
        rmSync(resolve(distDir, "src"), { recursive: true, force: true });
      }
    },
  };
}

export default defineConfig({
  plugins: [extensionAssets()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        background: resolve(root, "src/background.ts"),
        popup: resolve(root, "src/popup/popup.html"),
      },
      output: {
        // ES module output: MV3 service workers load as modules when the
        // manifest sets background.type = "module" (rolldown/rollup can't
        // code-split multiple non-module formats like iife/umd).
        entryFileNames: "[name].js",
      },
    },
  },
});
