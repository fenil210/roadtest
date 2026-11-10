// Render an HTML scene to video, one frame at a time, through headless Chrome and ffmpeg.
//
//   node media/render.mjs media/scenes/demo.html media/out/demo.mp4 --width 1640 --height 1040
//
// A scene sets window.DURATION (seconds) and window.seek(t), which draws the frame for time t.
// Nothing in a scene may depend on the wall clock, so every render is identical.
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [scene, output, ...flags] = process.argv.slice(2);
const option = (name, fallback) => {
  const index = flags.indexOf(`--${name}`);
  return index >= 0 ? flags[index + 1] : fallback;
};
const width = Number(option("width", 1920));
const height = Number(option("height", 1080));
const fps = Number(option("fps", 30));
const scale = Number(option("scale", 1));
const still = option("still", null); // --still 12.5 writes one PNG of that moment instead of a video

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
  args: ["--hide-scrollbars", "--force-color-profile=srgb", "--allow-file-access-from-files"],
});
const page = await browser.newPage();
page.on("pageerror", (error) => console.error("scene error:", error.message));
await page.setViewport({ width, height, deviceScaleFactor: scale });
if (flags.includes("--dark")) await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
const hash = option("hash", "");
await page.goto(pathToFileURL(resolve(scene)).href + (hash ? `#${hash}` : ""), { waitUntil: "load" });
await page.evaluate(() => document.fonts.ready);

// --cues: write the scene's soundtrack cues (window.CUES) to the output path as JSON.
if (flags.includes("--cues")) {
  const { writeFile } = await import("node:fs/promises");
  await writeFile(output, JSON.stringify(await page.evaluate(() => window.CUES()), null, 1));
  await browser.close();
  console.log(`${output}: cues`);
  process.exit(0);
}

// --still T: one PNG of a scene at time T. For a plain page (no window.seek), T is ignored.
if (still !== null) {
  await page.evaluate((t) => (typeof window.seek === "function" ? window.seek(t) : null), Number(still));
  await new Promise((done) => setTimeout(done, 300));
  if (flags.includes("--top")) await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: output, type: "png", fullPage: flags.includes("--full") });
  await browser.close();
  console.log(`${output}: still at ${still}s`);
  process.exit(0);
}

const duration = Number(option("duration", await page.evaluate(() => window.DURATION)));
const frames = Math.round(duration * fps);
const ffmpeg = spawn(
  "ffmpeg",
  ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-i", "-",
   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "17", "-preset", "slow", "-movflags", "+faststart",
   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", output],
  { stdio: ["pipe", "inherit", "inherit"] },
);
for (let frame = 0; frame < frames; frame++) {
  await page.evaluate((t) => window.seek(t), frame / fps);
  const image = await page.screenshot({ type: "png", optimizeForSpeed: true });
  if (!ffmpeg.stdin.write(image)) await new Promise((done) => ffmpeg.stdin.once("drain", done));
}
ffmpeg.stdin.end();
await new Promise((done) => ffmpeg.on("close", done));
await browser.close();
console.log(`${output}: ${frames} frames, ${duration}s at ${fps}fps, ${width}x${height}`);
