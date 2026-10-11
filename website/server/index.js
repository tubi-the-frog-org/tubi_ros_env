import express from 'express';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { writeFileSync, existsSync, appendFileSync, mkdirSync } from 'node:fs';
import os from 'node:os';
import { WebSocketServer } from 'ws';
import { SessionLock } from './session-lock.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');
const distDir = path.join(rootDir, 'dist');
const transcriptOutputPath = path.join(rootDir, 'output', 'transcripts.jsonl');

const appendTranscriptFile = (text) => {
  const outputDir = path.dirname(transcriptOutputPath);
  mkdirSync(outputDir, { recursive: true });
  appendFileSync(
    transcriptOutputPath,
    `${JSON.stringify({ timestamp: new Date().toISOString(), text })}\n`,
    'utf8'
  );
};

const resetTranscriptFile = () => {
  mkdirSync(path.dirname(transcriptOutputPath), { recursive: true });
  writeFileSync(transcriptOutputPath, '', 'utf8');
};

// Must match the exact string in pi_app's system prompt (decision_client.py).
const NO_SPEECH_MARKER = '(no speech detected)';
const STT_FAILURE_TEXT = /^(Speech transcription failed|No audio|I could not understand the audio)/;

// Speech-to-text. With STT_API_KEY (or LLM_API_KEY) set, audio goes to an
// OpenAI-compatible /audio/transcriptions endpoint: no Whisper/torch on the Pi
// (a Pi 3's 905 MiB can't afford it). Without a key it falls back to the
// local Whisper worker, which is fine on a dev laptop.
const sttApiKey = process.env.STT_API_KEY || process.env.LLM_API_KEY || '';
const sttBaseUrl = (process.env.STT_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '');
const sttModel = process.env.STT_MODEL || 'gpt-4o-mini-transcribe';

const transcribeInCloud = async (audioBuffer, mimeType, extension) => {
  const form = new FormData();
  form.append('file', new Blob([audioBuffer], { type: mimeType }), `speech.${extension}`);
  form.append('model', sttModel);
  form.append('language', 'en');
  const response = await fetch(`${sttBaseUrl}/audio/transcriptions`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${sttApiKey}` },
    body: form,
    signal: AbortSignal.timeout(20_000)
  });
  if (!response.ok) {
    throw new Error(`cloud transcription failed (HTTP ${response.status})`);
  }
  const body = await response.json();
  return { text: String(body.text ?? '').trim() };
};

const transcribeWithLocalWhisper = async (audioBuffer, extension) => {
  const audioPath = path.join(os.tmpdir(), `tubi-audio-${Date.now()}.${extension}`);
  writeFileSync(audioPath, audioBuffer);
  const pythonScript = path.join(rootDir, 'python', 'stt_worker.py');
  return new Promise((resolve, reject) => {
    const child = spawn('.venv/bin/python', [pythonScript, audioPath], { cwd: rootDir, env: process.env });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.on('close', (code) => {
      if (code !== 0) return reject(new Error(stderr || 'python transcription failed'));
      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error('invalid json response from python stt worker'));
      }
    });
  });
};

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server });

const getDuration = (name, fallback) => {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
};

const sessionLock = new SessionLock({
  idleTimeoutMs: getDuration('SESSION_IDLE_TIMEOUT_MS', 60_000),
  maxDurationMs: getDuration('SESSION_MAX_DURATION_MS', 180_000)
});
let devBusySessionId = null;

const clearExpiredDevBusy = () => {
  if (devBusySessionId && !sessionLock.isOwner(devBusySessionId)) {
    devBusySessionId = null;
  }
};

const broadcast = (type, payload = {}) => {
  const message = JSON.stringify({ type, payload, sentAt: Date.now() });
  for (const client of wss.clients) {
    if (client.readyState === 1) {
      client.send(message);
    }
  }
};

// The Pi-side service (pi_app/teammate_client.py) connects with ?role=teammate.
// It gets the conversation events the browsers must not see (a browser would
// render a guest transcript as a duplicate line).
const toTeammate = (type, payload = {}) => {
  const message = JSON.stringify({ type, payload, sentAt: Date.now() });
  for (const client of wss.clients) {
    if (client.readyState === 1 && client.role === 'teammate') {
      client.send(message);
    }
  }
};

// Guests may not talk while Tubi is thinking or speaking. Set when a turn is
// handed to the Pi, cleared when the Pi reports it finished ({"type":"state",
// "payload":{"speaking":false}}), the Pi disconnects, the session ends, or
// the safety timeout fires (so a crashed turn can never lock the guest out).
const TUBI_TURN_TIMEOUT_MS = Number(process.env.TUBI_TURN_TIMEOUT_MS) || 60_000;
let tubiBusy = false;
let tubiBusyTimer = null;

const teammateConnected = () =>
  [...wss.clients].some((client) => client.readyState === 1 && client.role === 'teammate');

const setTubiBusy = (busy) => {
  clearTimeout(tubiBusyTimer);
  if (busy) {
    tubiBusyTimer = setTimeout(() => setTubiBusy(false), TUBI_TURN_TIMEOUT_MS);
    tubiBusyTimer.unref();
  }
  if (busy === tubiBusy) return;
  tubiBusy = busy;
  broadcast('tubi-state', { busy });
};

const finishSession = () => {
  resetTranscriptFile();
  toTeammate('session:end');
  setTubiBusy(false);
};

const publishState = () => {
  broadcast('session-state', sessionLock.snapshot());
};

const expiryTimer = setInterval(() => {
  if (sessionLock.expireIfNeeded()) {
    clearExpiredDevBusy();
    finishSession();
    publishState();
  }
}, 5_000);
expiryTimer.unref();

app.use(express.json({ limit: '10mb' }));

app.get('/api/health', (_, res) => {
  if (sessionLock.expireIfNeeded()) {
    finishSession();
    publishState();
  }
  clearExpiredDevBusy();
  res.json({ ok: true, ...sessionLock.snapshot(), devBusy: Boolean(devBusySessionId) });
});

app.post('/api/session/start', (_, res) => {
  if (sessionLock.expireIfNeeded()) {
    finishSession();
    publishState();
  }
  const session = sessionLock.start();
  if (!session) {
    return res.status(409).json({ ok: false, error: 'frog busy' });
  }

  resetTranscriptFile();
  toTeammate('session:start', { sessionId: session.sessionId });
  if (teammateConnected()) setTubiBusy(true);
  publishState();
  res.json({ ok: true, ...session, status: 'active' });
});

app.post('/api/session/resume', (req, res) => {
  if (sessionLock.expireIfNeeded()) {
    finishSession();
    publishState();
  }
  const session = sessionLock.resume(req.body?.sessionId);
  if (!session) {
    return res.status(409).json({ ok: false, error: 'session expired or not owned by this tab' });
  }

  res.json({ ok: true, ...session, status: 'active' });
});

app.post('/api/session/heartbeat', (req, res) => {
  if (sessionLock.expireIfNeeded()) {
    finishSession();
    publishState();
  }
  if (!sessionLock.heartbeat(req.body?.sessionId)) {
    return res.status(409).json({ ok: false, error: 'session expired or not owned by this tab' });
  }

  res.json({ ok: true });
});

app.post('/api/session/end', (req, res) => {
  if (sessionLock.expireIfNeeded()) {
    finishSession();
    publishState();
  }
  if (!sessionLock.end(req.body?.sessionId)) {
    return res.status(409).json({ ok: false, error: 'session expired or not owned by this tab' });
  }

  finishSession();
  publishState();
  res.json({ ok: true, status: 'idle' });
});

app.post('/api/session/reset', (_, res) => {
  if (process.env.NODE_ENV === 'production') {
    return res.status(404).json({ ok: false, error: 'not found' });
  }

  sessionLock.forceEnd();
  devBusySessionId = null;
  finishSession();
  publishState();
  res.json({ ok: true, status: 'idle' });
});

app.post('/api/dev/session/busy', (req, res) => {
  if (process.env.NODE_ENV === 'production') {
    return res.status(404).json({ ok: false, error: 'not found' });
  }

  if (sessionLock.expireIfNeeded()) {
    finishSession();
    publishState();
  }
  clearExpiredDevBusy();

  if (req.body?.busy) {
    if (!devBusySessionId) {
      const session = sessionLock.start();
      if (!session) {
        return res.status(409).json({ ok: false, error: 'another session already owns the lock' });
      }
      devBusySessionId = session.sessionId;
      publishState();
    }
  } else if (devBusySessionId) {
    const released = sessionLock.end(devBusySessionId);
    devBusySessionId = null;
    if (released) publishState();
  }

  res.json({
    ok: true,
    devBusy: Boolean(devBusySessionId),
    ...sessionLock.snapshot()
  });
});

app.post('/api/transcribe', async (req, res) => {
  try {
    if (sessionLock.expireIfNeeded()) publishState();
    if (!sessionLock.heartbeat(req.body?.sessionId)) {
      return res.status(409).json({ ok: false, error: 'active session required' });
    }
    if (tubiBusy) {
      return res.status(409).json({ ok: false, error: 'Tubi is speaking', tubiBusy: true });
    }

    if (!req.body || (typeof req.body === 'object' && Object.keys(req.body).length === 0)) {
      return res.status(400).json({ ok: false, error: 'missing audio payload' });
    }

    let audioBuffer;
    let mimeType = 'audio/webm';
    let extension = 'webm';

    const candidate =
      (typeof req.body === 'object' && req.body !== null && 'audioBase64' in req.body)
        ? req.body.audioBase64
        : (typeof req.body === 'object' && req.body !== null && 'audio' in req.body)
          ? req.body.audio
          : undefined;

    if (Buffer.isBuffer(req.body)) {
      audioBuffer = req.body;
    } else if (typeof candidate === 'string') {
      mimeType = req.body.mimeType || 'audio/webm';
      extension = req.body.extension || (mimeType.includes('wav') ? 'wav' : 'webm');
      const base64Data = candidate.replace(/^data:audio\/[a-zA-Z0-9.+-]+;base64,/, '');
      audioBuffer = Buffer.from(base64Data, 'base64');
    } else if (typeof req.body === 'string') {
      audioBuffer = Buffer.from(req.body, 'base64');
    } else if (ArrayBuffer.isView(req.body) || req.body instanceof ArrayBuffer) {
      audioBuffer = Buffer.from(req.body);
    } else {
      audioBuffer = Buffer.from(JSON.stringify(req.body));
    }

    const result = sttApiKey
      ? await transcribeInCloud(audioBuffer, mimeType, extension)
      : await transcribeWithLocalWhisper(audioBuffer, extension);

    if (!sessionLock.heartbeat(req.body.sessionId)) {
      return res.status(409).json({ ok: false, error: 'session ended before transcription completed' });
    }

    const text = result.text || 'I did not catch that clearly.';
    appendTranscriptFile(text);
    // stt_worker.py reports its own failures as plain text; don't feed those to Tubi.
    // An empty result (nothing but silence/noise) IS forwarded, as a marker the
    // system prompt knows to answer with "what was that?" instead of leaving the
    // guest in dead air with a locked-out button.
    if (!STT_FAILURE_TEXT.test(result.text ?? '')) {
      if (teammateConnected()) setTubiBusy(true);
      toTeammate('transcript', { text: result.text || NO_SPEECH_MARKER, isFinal: true, timestamp: Date.now() });
    }
    res.json({ ok: true, text });
  } catch (error) {
    res.status(500).json({
      ok: false,
      error: error instanceof Error ? error.message : 'transcription failed'
    });
  }
});

app.post('/api/dev/display', (req, res) => {
  const {
    type = 'display',
    message = 'Tubi is showing a display update.',
    payload = {}
  } = req.body ?? {};

  broadcast(type, { message, ...payload });
  res.json({ ok: true, type, message, payload });
});

app.post('/api/dev/transcript', (req, res) => {
  const { text = 'Demo transcript from the fake backend.' } = req.body ?? {};
  broadcast('transcript', { text, source: 'fake-backend' });
  res.json({ ok: true, text });
});

wss.on('connection', (socket, req) => {
  const role = new URL(req.url ?? '/', 'http://localhost').searchParams.get('role');
  socket.role = role === 'teammate' ? 'teammate' : 'browser';
  socket.send(JSON.stringify({ type: 'session-state', payload: sessionLock.snapshot() }));
  socket.send(JSON.stringify({ type: 'tubi-state', payload: { busy: tubiBusy } }));
  socket.on('close', () => {
    if (socket.role === 'teammate' && !teammateConnected()) setTubiBusy(false);
  });

  socket.on('message', (raw) => {
    try {
      const data = JSON.parse(raw.toString());

      if (socket.role === 'teammate' && data.type === 'state' && data.payload?.speaking === false) {
        setTubiBusy(false);
        return;
      }

      if (data.type === 'transcript') {
        broadcast('transcript', { text: data.payload?.text ?? '' });
      }
    } catch (error) {
      // ignore malformed messages during early development
    }
  });
});

const serveClient = () => {
  if (existsSync(distDir)) {
    app.use(express.static(distDir));
    app.get('*', (req, res, next) => {
      if (req.path.startsWith('/api')) return next();
      res.sendFile(path.join(distDir, 'index.html'));
    });
  }
};

serveClient();

const port = Number(process.env.PORT || 3000);
server.listen(port, () => {
  console.log(`Tubi server listening on http://localhost:${port}`);
});
