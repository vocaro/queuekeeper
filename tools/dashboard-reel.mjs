// Render the dashboard reel with Chrome's SVG clock and encode it with FFmpeg.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { copyFile, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const assets = join(here, '../assets');
const chromePath = process.env.QUEUEKEEPER_REEL_CHROME ??
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const size = 1080;
const fps = 24;
const duration = 23;

function devTools(chrome) {
  const pending = new Map();
  let id = 0;
  let buffer = '';
  let failure;
  chrome.on('error', fail);
  chrome.on('exit', (code, signal) => fail(new Error(`Chrome exited (${signal ?? code}).`)));
  chrome.stdio[3].on('error', fail);
  chrome.stdio[4].setEncoding('utf8').on('data', chunk => {
    buffer += chunk;
    let end;
    while ((end = buffer.indexOf('\0')) !== -1) {
      const reply = JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
      const request = pending.get(reply.id);
      if (request) {
        pending.delete(reply.id);
        clearTimeout(request.timer);
        if (reply.error) request.reject(new Error(reply.error.message));
        else request.resolve(reply.result);
      }
    }
  });

  function fail(error) {
    failure = error;
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    pending.clear();
  }

  return (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    if (failure) return reject(failure);
    const requestId = ++id;
    const timer = setTimeout(() => {
      pending.delete(requestId);
      reject(new Error(`Chrome did not answer ${method}.`));
    }, 30_000);
    pending.set(requestId, { resolve, reject, timer });
    chrome.stdio[3].write(JSON.stringify({ id: requestId, method, params, sessionId }) + '\0');
  });
}

async function preparePage(scratch) {
  const template = await readFile(join(here, 'dashboard-reel.html'), 'utf8');
  const home = await readFile(join(assets, 'dashboard-home.svg'), 'utf8');
  const styles = home.match(/<style>([\s\S]*?)<\/style>/)?.[1];
  const map = home.match(/<svg\b[^>]*\bx="[^"]+"[^>]*>[\s\S]*?<\/svg>/)?.[0];
  if (!styles || !map?.includes('<animateMotion')) {
    throw new Error('Could not find the animated Flow map in assets/dashboard-home.svg.');
  }
  // Keep the viewBox, but remove the map's placement within the dashboard screenshot.
  const flow = map.replace(/^<svg\b[^>]*\bviewBox="([^"]+)"[^>]*>/,
    '<svg viewBox="$1" aria-label="Queuekeeper Flow map">');
  const page = join(scratch, 'reel.html');
  await writeFile(page, template.replace('/* FLOW_STYLES */', () => styles)
    .replace('<!-- FLOW_MAP -->', () => flow));
  for (const file of ['logo.svg', 'dashboard-farm.png', 'dashboard-history.png', 'dashboard-activity.png']) {
    await copyFile(join(assets, file), join(scratch, file));
  }
  return pathToFileURL(page).href;
}

async function render(scratch, destination) {
  const url = await preparePage(scratch);
  const chrome = spawn(chromePath, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
    '--disable-background-networking', '--remote-debugging-pipe',
    `--user-data-dir=${join(scratch, 'chrome')}`, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'inherit', 'pipe', 'pipe'] });
  // Wait for Chrome to release its profile before the caller removes it.
  const chromeClosed = new Promise(resolve => chrome.once('close', resolve));
  const call = devTools(chrome);
  let encoder;
  try {
    const { targetId } = await call('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await call('Target.attachToTarget', { targetId, flatten: true });
    const tab = (method, params) => call(method, params, sessionId);
    await tab('Emulation.setDeviceMetricsOverride', {
      width: size, height: size, deviceScaleFactor: 1, mobile: false,
    });
    await tab('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }],
    });
    await tab('Page.navigate', { url });
    const evaluate = async expression => {
      const reply = await tab('Runtime.evaluate', { expression, returnByValue: true });
      if (reply.exceptionDetails) {
        throw new Error(reply.exceptionDetails.exception?.description ?? reply.exceptionDetails.text);
      }
      return reply.result.value;
    };
    const deadline = Date.now() + 30_000;
    while (!await evaluate("document.readyState === 'complete' && typeof prepare === 'function'")) {
      if (Date.now() > deadline) throw new Error('Chrome did not load the reel page.');
      await new Promise(done => setTimeout(done, 100));
    }
    await evaluate('prepare()');

    encoder = spawn('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps),
      '-c:v', 'mjpeg', '-i', 'pipe:0', '-c:v', 'libx264', '-preset', 'fast', '-crf', '17',
      '-pix_fmt', 'yuv420p', '-movflags', '+faststart', destination,
    ], { stdio: ['pipe', 'inherit', 'inherit'] });
    const encoded = once(encoder, 'close').then(([code]) => {
      if (code !== 0) throw new Error(`FFmpeg exited with code ${code}.`);
    });
    // FFmpeg can fail before Chrome has delivered the next frame.
    encoded.catch(() => encoder.stdin.destroy());
    encoder.stdin.on('error', () => encoder.kill());
    for (let index = 0; index < duration * fps; index++) {
      await evaluate(`frame(${index / fps})`);
      const { data } = await tab('Page.captureScreenshot', { format: 'jpeg', quality: 92 });
      if (!encoder.stdin.write(Buffer.from(data, 'base64'))) {
        await Promise.race([once(encoder.stdin, 'drain'), encoded]);
      }
    }
    encoder.stdin.end();
    await encoded;
  } finally {
    encoder?.kill();
    chrome.kill();
    await chromeClosed;
  }
}

const output = resolve(process.argv[2] ?? join(assets, 'queuekeeper-sizzle.mp4'));
const scratch = await mkdtemp(join(tmpdir(), 'queuekeeper-reel-'));
let staging;
try {
  staging = await mkdtemp(join(dirname(output), '.queuekeeper-reel-'));
  const video = join(staging, 'reel.mp4');
  await render(scratch, video);
  await rename(video, output);
  console.log(output);
} finally {
  if (staging) await rm(staging, { recursive: true, force: true });
  await rm(scratch, { recursive: true, force: true, maxRetries: 5 });
}
