import { useEffect, useRef, useState } from 'react'
import Sprite from './Sprite.jsx'
import ThreadPanel from './ThreadPanel.jsx'
import { fetchThreads, forgetThread, updateSprite } from './api.js'

const SPRITE_SIZE = 80
const TAP_MOVE_THRESHOLD = 6
// Wander model: each bot moves at a constant speed along a heading that
// drifts via a small random walk. A gentle steering force biases the heading
// back toward home zone so they orbit it instead of escaping. They never stop.
const HOME_STEER = 0.025          // strength of "turn toward home" per frame
const HOME_RADIUS = 90            // within this distance no home steering applied

const WORLD_NAMES = ['forest', 'beach', 'meadow', 'factory']

// Home zones — relative coordinates inside the world. Same across worlds.
// Each Slack channel is assigned to one zone by a stable hash of its name.
const ZONES = {
  north: { cx: 0.22, cy: 0.32, color: '#7aa9ff', label: 'north' },
  east:  { cx: 0.78, cy: 0.30, color: '#ffd166', label: 'east'  },
  west:  { cx: 0.22, cy: 0.74, color: '#c084fc', label: 'west'  },
  south: { cx: 0.78, cy: 0.72, color: '#5dd1a6', label: 'south' },
}

function detectProject(channel) {
  if (!channel) return null
  let h = 0
  for (const ch of channel) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  const keys = Object.keys(ZONES)
  return keys[h % keys.length]
}

function rand(min, max) { return min + Math.random() * (max - min) }

function speedFor(status) {
  if (status === 'working') return 1.4    // ~84 px/sec
  if (status === 'idle')    return 0.7    // ~42 px/sec
  return 0                                // waiting_on_user: stays put, just pulses
}

function headingDriftFor(status) {
  if (status === 'working') return 0.045  // wider random turns
  if (status === 'idle')    return 0.025  // gentler turns
  return 0
}

function shortestAngleDiff(a, b) {
  let d = a - b
  while (d > Math.PI)  d -= 2 * Math.PI
  while (d < -Math.PI) d += 2 * Math.PI
  return d
}

const DEMO_THREADS = [
  { thread_key: 'demo-1', status: 'working',         channel_name: 'survey-app', summary: 'adding consent page',
    sprite: { kind: 'blob',     hue: 200, name: 'NEKO' } },
  { thread_key: 'demo-2', status: 'idle',            channel_name: 'survey-app', summary: 'fix mobile layout',
    sprite: { kind: 'ghost',    hue: 220, name: 'WIM'  } },
  { thread_key: 'demo-3', status: 'working',         channel_name: 'analysis',   summary: 'cleaning raw data',
    sprite: { kind: 'cat',      hue: 40,  name: 'POG'  } },
  { thread_key: 'demo-4', status: 'idle',            channel_name: 'analysis',   summary: 'plot by condition',
    sprite: { kind: 'mushroom', hue: 30,  name: 'MIM'  } },
  { thread_key: 'demo-5', status: 'working',         channel_name: 'docs',       summary: 'update README',
    sprite: { kind: 'bird',     hue: 280, name: 'FUFU' } },
  { thread_key: 'demo-6', status: 'waiting_on_user', channel_name: 'website',    summary: 'needs review',
    sprite: { kind: 'slime',    hue: 160, name: 'MOMO' } },
  { thread_key: 'demo-7', status: 'idle',            channel_name: 'website',    summary: 'follow-up design',
    sprite: { kind: 'cat',      hue: 140, name: 'BUBU' } },
  { thread_key: 'demo-8', status: 'idle',            channel_name: 'misc',       summary: 'wandering',
    sprite: { kind: 'blob',     hue: 320, name: 'GLU'  } },
]

// Per-world scenery. Same coordinate slots across worlds where possible so
// the layout stays balanced across the project zones.
const DECORATIONS_BY_WORLD = {
  forest: [
    { type: 'pine-large', x: 0.04, y: 0.56 },
    { type: 'pine',       x: 0.13, y: 0.50 },
    { type: 'pine-large', x: 0.96, y: 0.58 },
    { type: 'pine',       x: 0.88, y: 0.51 },
    { type: 'pine',       x: 0.50, y: 0.44 },
    { type: 'pond',       x: 0.50, y: 0.52 },
    { type: 'lilypad',    x: 0.46, y: 0.54 },
    { type: 'lilypad',    x: 0.54, y: 0.51 },
    { type: 'bush',       x: 0.18, y: 0.94 },
    { type: 'bush',       x: 0.40, y: 0.96 },
    { type: 'bush',       x: 0.62, y: 0.95 },
    { type: 'bush',       x: 0.84, y: 0.93 },
    { type: 'mushrooms',  x: 0.32, y: 0.86 },
    { type: 'mushrooms',  x: 0.70, y: 0.84 },
    { type: 'flower',     x: 0.14, y: 0.80 },
    { type: 'flower',     x: 0.86, y: 0.78 },
    { type: 'flower',     x: 0.46, y: 0.88 },
    { type: 'flower',     x: 0.55, y: 0.96 },
    { type: 'flower',     x: 0.27, y: 0.94 },
    { type: 'flower',     x: 0.74, y: 0.96 },
    { type: 'flower',     x: 0.10, y: 0.92 },
    { type: 'flower',     x: 0.92, y: 0.90 },
  ],
  beach: [
    { type: 'palm-large', x: 0.07, y: 0.55 },
    { type: 'palm-large', x: 0.93, y: 0.57 },
    { type: 'palm',       x: 0.16, y: 0.50 },
    { type: 'palm',       x: 0.84, y: 0.50 },
    // foreground
    { type: 'driftwood',  x: 0.32, y: 0.92 },
    { type: 'driftwood',  x: 0.68, y: 0.93 },
    { type: 'sand-tuft',  x: 0.20, y: 0.88 },
    { type: 'sand-tuft',  x: 0.80, y: 0.86 },
    { type: 'sand-tuft',  x: 0.45, y: 0.94 },
    { type: 'sand-tuft',  x: 0.58, y: 0.96 },
    { type: 'shell',      x: 0.14, y: 0.92 },
    { type: 'shell',      x: 0.86, y: 0.94 },
    { type: 'shell',      x: 0.50, y: 0.92 },
    { type: 'starfish',   x: 0.40, y: 0.96 },
    { type: 'starfish',   x: 0.62, y: 0.95 },
    { type: 'pebble',     x: 0.27, y: 0.95 },
    { type: 'pebble',     x: 0.74, y: 0.95 },
    { type: 'pebble',     x: 0.10, y: 0.96 },
    { type: 'pebble',     x: 0.92, y: 0.96 },
  ],
  meadow: [
    // distant rolling hills
    { type: 'hill-far',   x: 0.20, y: 0.55 },
    { type: 'hill-far',   x: 0.80, y: 0.55 },
    { type: 'oak',        x: 0.10, y: 0.55 },
    { type: 'oak-small',  x: 0.92, y: 0.52 },
    { type: 'oak-small',  x: 0.50, y: 0.48 },
    // foreground
    { type: 'tall-grass', x: 0.10, y: 0.88 },
    { type: 'tall-grass', x: 0.32, y: 0.93 },
    { type: 'tall-grass', x: 0.68, y: 0.92 },
    { type: 'tall-grass', x: 0.90, y: 0.90 },
    { type: 'wildflower-red',    x: 0.16, y: 0.80 },
    { type: 'wildflower-yellow', x: 0.84, y: 0.78 },
    { type: 'wildflower-blue',   x: 0.46, y: 0.88 },
    { type: 'wildflower-yellow', x: 0.55, y: 0.96 },
    { type: 'wildflower-red',    x: 0.27, y: 0.94 },
    { type: 'wildflower-blue',   x: 0.74, y: 0.96 },
    { type: 'wildflower-red',    x: 0.40, y: 0.92 },
    { type: 'wildflower-yellow', x: 0.62, y: 0.94 },
    { type: 'butterfly',  x: 0.45, y: 0.40 },
    { type: 'butterfly',  x: 0.60, y: 0.30 },
  ],
  factory: [
    // distant skyline
    { type: 'building',         x: 0.10, y: 0.55 },
    { type: 'building-tall',    x: 0.90, y: 0.55 },
    { type: 'building',         x: 0.50, y: 0.55 },
    // smokestacks rising above the skyline
    { type: 'smokestack-large', x: 0.04, y: 0.48 },
    { type: 'smokestack',       x: 0.95, y: 0.50 },
    { type: 'smokestack',       x: 0.62, y: 0.51 },
    // steam puffs above each stack
    { type: 'steam',            x: 0.04, y: 0.32 },
    { type: 'steam-b',          x: 0.95, y: 0.34 },
    { type: 'steam-c',          x: 0.62, y: 0.36 },
    // mid-air mechanical accents
    { type: 'gear-deco',        x: 0.50, y: 0.50 },
    { type: 'gear-deco-small',  x: 0.43, y: 0.55 },
    { type: 'pipe',             x: 0.50, y: 0.62 },
    { type: 'steel-beam',       x: 0.30, y: 0.46 },
    { type: 'steel-beam',       x: 0.70, y: 0.46 },
    // foreground floor clutter
    { type: 'conveyor',         x: 0.32, y: 0.92 },
    { type: 'conveyor',         x: 0.68, y: 0.92 },
    { type: 'crate',            x: 0.18, y: 0.93 },
    { type: 'crate',            x: 0.84, y: 0.93 },
    { type: 'crate-stack',      x: 0.45, y: 0.91 },
    { type: 'drum-red',         x: 0.10, y: 0.95 },
    { type: 'drum-blue',        x: 0.92, y: 0.94 },
    { type: 'drum-yellow',      x: 0.55, y: 0.95 },
    { type: 'drum-red',         x: 0.27, y: 0.96 },
    { type: 'drum-blue',        x: 0.73, y: 0.96 },
    // glowing lamps (especially nice at night)
    { type: 'lamp-post',        x: 0.40, y: 0.84 },
    { type: 'lamp-post',        x: 0.60, y: 0.84 },
  ],
}

export default function App() {
  const [threads, setThreads] = useState([])
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)
  const [world, setWorld] = useState(() => {
    const params = new URLSearchParams(window.location.search)
    const fromUrl = params.get('world')
    if (WORLD_NAMES.includes(fromUrl)) return fromUrl
    const w = localStorage.getItem('pip:world')
    return WORLD_NAMES.includes(w) ? w : 'forest'
  })
  const [time, setTime] = useState(() => {
    const params = new URLSearchParams(window.location.search)
    const fromUrl = params.get('time')
    if (fromUrl === 'day' || fromUrl === 'night') return fromUrl
    const t = localStorage.getItem('pip:time')
    return t === 'day' || t === 'night' ? t : 'night'
  })
  const [chatFont, setChatFont] = useState(
    () => localStorage.getItem('pip:chatFont') || 'mono'
  )
  const [panelWidth, setPanelWidth] = useState(
    () => Number(localStorage.getItem('pip:panelWidth')) || 560
  )
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [worldOpen, setWorldOpen] = useState(false)
  const [confirmKillAll, setConfirmKillAll] = useState(false)

  useEffect(() => { localStorage.setItem('pip:world', world) }, [world])
  useEffect(() => { localStorage.setItem('pip:time', time) },  [time])
  useEffect(() => { localStorage.setItem('pip:chatFont', chatFont) }, [chatFont])
  useEffect(() => { localStorage.setItem('pip:panelWidth', String(panelWidth)) }, [panelWidth])

  // Mirror theme on <html> so margins / safe areas paint the right color too.
  useEffect(() => {
    document.documentElement.className = `${world} ${time}`
  }, [world, time])

  // Push chat-font + panel-width to CSS variables on <html>.
  useEffect(() => {
    const v = chatFont === 'sans'
      ? "-apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif"
      : "'SF Mono', Menlo, ui-monospace, monospace"
    document.documentElement.style.setProperty('--chat-font', v)
  }, [chatFont])
  useEffect(() => {
    document.documentElement.style.setProperty('--panel-width', `${panelWidth}px`)
  }, [panelWidth])

  const worldRef = useRef(null)
  const elementsRef = useRef({})
  const stateRef = useRef({})
  const dragRef = useRef(null)

  const isDemo = new URLSearchParams(window.location.search).get('demo') === '1'

  async function refresh() {
    try {
      const data = await fetchThreads()
      setThreads(data.threads || [])
      setError(null)
    } catch (e) { setError(String(e)) }
  }

  useEffect(() => {
    if (isDemo) {
      setThreads(DEMO_THREADS)
      return
    }
    refresh()
    const id = setInterval(refresh, 3000)
    return () => clearInterval(id)
  }, [isDemo])

  useEffect(() => {
    if (!worldRef.current) return
    const W = worldRef.current.clientWidth
    const H = worldRef.current.clientHeight
    const live = new Set(threads.map(t => t.thread_key))
    for (const k of Object.keys(stateRef.current)) {
      if (!live.has(k)) {
        delete stateRef.current[k]
        delete elementsRef.current[k]
      }
    }
    for (const t of threads) {
      const project = detectProject(t.channel_name)
      const home = project ? ZONES[project] : null
      const pa = t.sprite?.parked_at || null
      const cur = stateRef.current[t.thread_key]
      if (!cur) {
        const startX = pa ? pa.x * W - SPRITE_SIZE / 2
          : (home ? home.cx * W - SPRITE_SIZE / 2 : rand(0, Math.max(0, W - SPRITE_SIZE)))
        const startY = pa ? pa.y * H - SPRITE_SIZE / 2
          : (home ? home.cy * H - SPRITE_SIZE / 2 : rand(0, Math.max(0, H - SPRITE_SIZE)))
        stateRef.current[t.thread_key] = {
          x: pa ? startX : startX + rand(-110, 110),
          y: pa ? startY : startY + rand(-90, 90),
          heading: Math.random() * Math.PI * 2,
          status: t.status,
          home,
          project,
          parked: pa ? { x: pa.x, y: pa.y } : null,
          dragging: false,
        }
      } else {
        cur.status = t.status
        cur.home = home
        cur.project = project
        // Server is source of truth for parked state, but don't fight an active drag.
        if (!cur.dragging) cur.parked = pa ? { x: pa.x, y: pa.y } : null
      }
    }
  }, [threads])

  useEffect(() => {
    let raf
    const tick = () => {
      const w = worldRef.current
      if (!w) { raf = requestAnimationFrame(tick); return }
      const W = w.clientWidth
      const H = w.clientHeight
      const maxX = Math.max(0, W - SPRITE_SIZE)
      const maxY = Math.max(0, H - SPRITE_SIZE)
      for (const [k, p] of Object.entries(stateRef.current)) {
        if (p.dragging) {
          // pointer handler owns x/y while dragging
        } else if (p.parked) {
          p.x = p.parked.x * W - SPRITE_SIZE / 2
          p.y = p.parked.y * H - SPRITE_SIZE / 2
        } else if (p.status !== 'waiting_on_user') {
          // Random walk on heading — keeps direction always changing.
          const drift = headingDriftFor(p.status)
          p.heading += rand(-drift, drift)

          // Soft steering toward home when too far from it.
          if (p.home) {
            const cx = p.x + SPRITE_SIZE / 2
            const cy = p.y + SPRITE_SIZE / 2
            const dx = p.home.cx * W - cx
            const dy = p.home.cy * H - cy
            const dist = Math.hypot(dx, dy)
            if (dist > HOME_RADIUS) {
              const homeAngle = Math.atan2(dy, dx)
              p.heading += shortestAngleDiff(homeAngle, p.heading) * HOME_STEER
            }
          }

          // Constant-speed motion: never stops.
          const speed = speedFor(p.status)
          p.x += Math.cos(p.heading) * speed
          p.y += Math.sin(p.heading) * speed

          // Reflect heading off walls.
          if (p.x < 0)        { p.x = 0;     p.heading = Math.PI - p.heading }
          else if (p.x > maxX){ p.x = maxX;  p.heading = Math.PI - p.heading }
          if (p.y < 0)        { p.y = 0;     p.heading = -p.heading }
          else if (p.y > maxY){ p.y = maxY;  p.heading = -p.heading }
        }
        const el = elementsRef.current[k]
        if (el) el.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px)`
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])

  function onPointerDown(e) {
    const key = e.currentTarget.dataset.key
    const rect = worldRef.current?.getBoundingClientRect()
    if (!rect) return
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = {
      key,
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      rectLeft: rect.left,
      rectTop: rect.top,
      isDragging: false,
    }
  }

  function onPointerMove(e) {
    const d = dragRef.current
    if (!d || d.pointerId !== e.pointerId) return
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    if (!d.isDragging) {
      if (Math.hypot(dx, dy) <= TAP_MOVE_THRESHOLD) return
      d.isDragging = true
      const p = stateRef.current[d.key]
      if (p) p.dragging = true
    }
    const p = stateRef.current[d.key]
    if (!p) return
    p.x = e.clientX - d.rectLeft - SPRITE_SIZE / 2
    p.y = e.clientY - d.rectTop  - SPRITE_SIZE / 2
  }

  function onPointerUp(e) {
    const d = dragRef.current
    if (!d || d.pointerId !== e.pointerId) return
    dragRef.current = null
    if (!d.isDragging) {
      setOpen(d.key)
      return
    }
    const p = stateRef.current[d.key]
    const w = worldRef.current
    if (!p || !w) return
    p.dragging = false
    const W = w.clientWidth
    const H = w.clientHeight
    if (W <= 0 || H <= 0) return
    const nx = Math.max(0, Math.min(1, (p.x + SPRITE_SIZE / 2) / W))
    const ny = Math.max(0, Math.min(1, (p.y + SPRITE_SIZE / 2) / H))
    p.parked = { x: nx, y: ny }
    updateSprite(d.key, { parked_at: { x: nx, y: ny } }).catch(err => setError(String(err)))
  }

  async function killAll() {
    const keys = threads.map(t => t.thread_key)
    setConfirmKillAll(false)
    setOpen(null)
    try {
      await Promise.all(keys.map(k => forgetThread(k)))
    } catch (e) {
      setError(String(e))
    }
    refresh()
  }

  const counts = threads.reduce((acc, t) => {
    acc[t.status] = (acc[t.status] || 0) + 1
    return acc
  }, {})

  const decorations = DECORATIONS_BY_WORLD[world] || DECORATIONS_BY_WORLD.forest
  const toggleTime = () => setTime(time === 'day' ? 'night' : 'day')
  const toggleFont = () => setChatFont(chatFont === 'mono' ? 'sans' : 'mono')

  return (
    <div className={`app ${world} ${time}`}>
      <div className="header">
        <h1>pip</h1>
        <div className="meta">
          {threads.length} thread{threads.length === 1 ? '' : 's'}
          {counts.working ? ` · ${counts.working} working` : ''}
          {counts.waiting_on_user ? ` · ${counts.waiting_on_user} waiting on you` : ''}
        </div>
        <div className="controls">
          <div className="settings-wrap">
            <button
              className="pill"
              onClick={() => { setWorldOpen(o => !o); setSettingsOpen(false) }}
              title="change world"
              aria-expanded={worldOpen}
            >
              <span className="pill-icon"><WorldIcon world={world} /></span>
              {world}
            </button>
            {worldOpen && (
              <div className="settings-pop world-pop" role="dialog">
                {WORLD_NAMES.map(w => (
                  <button
                    key={w}
                    className={`world-choice ${w === world ? 'on' : ''}`}
                    onClick={() => { setWorld(w); setWorldOpen(false) }}
                  >
                    <span className="world-choice-icon"><WorldIcon world={w} /></span>
                    <span className="world-choice-label">{w}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button className="pill" onClick={toggleTime} title="toggle day/night">
            <span className="pill-icon">{time === 'day' ? <SunIcon /> : <MoonIcon />}</span>
            {time}
          </button>
          <button className="pill" onClick={toggleFont} title="toggle chat font">
            {chatFont === 'mono' ? 'mono' : 'sans'}
          </button>
          {threads.length > 0 && (
            confirmKillAll ? (
              <>
                <button
                  className="pill pill-danger"
                  onClick={killAll}
                  title="forget every sprite"
                >kill {threads.length}?</button>
                <button
                  className="pill"
                  onClick={() => setConfirmKillAll(false)}
                  title="cancel"
                >cancel</button>
              </>
            ) : (
              <button
                className="pill"
                onClick={() => setConfirmKillAll(true)}
                title="forget every sprite"
              >💀 kill all</button>
            )
          )}
          <div className="settings-wrap">
            <button
              className="pill"
              onClick={() => { setSettingsOpen(o => !o); setWorldOpen(false) }}
              title="settings"
              aria-expanded={settingsOpen}
            >
              ⚙
            </button>
            {settingsOpen && (
              <div className="settings-pop" role="dialog">
                <div className="settings-row">
                  <span className="settings-label">panel width</span>
                  <div className="seg">
                    {[440, 560, 720, 880].map(w => (
                      <button
                        key={w}
                        className={panelWidth === w ? 'on' : ''}
                        onClick={() => setPanelWidth(w)}
                      >{w}</button>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className={`world ${world} ${time}`} ref={worldRef}>
        {time === 'night' && <div className="celestial moon" />}
        {time === 'day'   && <div className="celestial sun" />}
        {time === 'night' && <div className="stars" />}
        {time === 'day'   && <div className="clouds" />}
        <div className="ground" />
        {error && <div className="error-banner">⚠ {error}</div>}

        {Object.entries(ZONES).map(([key, z]) => (
          <div
            key={`aura-${key}`}
            className="zone-aura"
            style={{ left: `${z.cx * 100}%`, top: `${z.cy * 100}%`, '--zone-color': z.color }}
          />
        ))}

        {decorations.map((d, i) => (
          <div
            key={`deco-${world}-${i}`}
            className={`decoration deco-${d.type}`}
            style={{ left: `${d.x * 100}%`, top: `${d.y * 100}%` }}
          >
            <Decoration type={d.type} />
          </div>
        ))}

        {Object.entries(ZONES).map(([key, z]) => (
          <div
            key={`landmark-${key}`}
            className="landmark"
            style={{ left: `${z.cx * 100}%`, top: `${z.cy * 100}%`, '--zone-color': z.color }}
          >
            <Landmark project={key} />
            <span className="zone-label">{z.label}</span>
          </div>
        ))}

        {threads.length === 0 && !error && (
          <div className="empty world-empty">no threads yet — send a message to claude in slack</div>
        )}
        {threads.map(t => (
          <div
            key={t.thread_key}
            data-key={t.thread_key}
            ref={el => { if (el) elementsRef.current[t.thread_key] = el }}
            className={`sprite-cell ${t.status}`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            title={t.summary}
          >
            <div className="ground-shadow" />
            <Sprite kind={t.sprite.kind} hue={t.sprite.hue} />
            <div className="name">{t.sprite.name}</div>
            <div className="channel">#{t.channel_name || 'dm'}</div>
          </div>
        ))}
      </div>

      {open && (
        <ThreadPanel
          threadKey={open}
          onClose={() => setOpen(null)}
          onForgot={() => { setOpen(null); refresh() }}
          onSpriteChange={refresh}
        />
      )}
    </div>
  )
}

// ─── Header icons ─────────────────────────────────────────────────────────────

function SunIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12">
      <circle cx="6" cy="6" r="2.5" fill="#ffd166" />
      <g stroke="#ffd166" strokeWidth="1.2" strokeLinecap="round">
        <line x1="6" y1="0.5" x2="6"  y2="2" />
        <line x1="6" y1="10"  x2="6"  y2="11.5" />
        <line x1="0.5" y1="6" x2="2"  y2="6" />
        <line x1="10"  y1="6" x2="11.5" y2="6" />
        <line x1="2" y1="2"   x2="3"  y2="3" />
        <line x1="9" y1="9"   x2="10" y2="10" />
        <line x1="9" y1="2"   x2="10" y2="3" />
        <line x1="2" y1="9"   x2="3"  y2="10" />
      </g>
    </svg>
  )
}

function WorldIcon({ world }) {
  switch (world) {
    case 'forest': return (
      <svg width="14" height="14" viewBox="0 0 14 14" shapeRendering="crispEdges">
        <polygon points="7,1 3,7 11,7" fill="#3a7245" />
        <polygon points="7,4 2,11 12,11" fill="#4a8a4f" />
        <rect x="6" y="11" width="2" height="3" fill="#3a2a18" />
      </svg>
    )
    case 'beach': return (
      <svg width="14" height="14" viewBox="0 0 14 14">
        <rect x="0" y="9" width="14" height="5" fill="#ecbd72" />
        <rect x="0" y="7" width="14" height="2" fill="#5d9bbf" />
        <path d="M 7 9 Q 8 5 10 1" stroke="#7a5630" strokeWidth="1" fill="none" strokeLinecap="round" />
        <path d="M 10 1 Q 7 1 5 3" stroke="#3a7245" strokeWidth="1.4" fill="none" strokeLinecap="round" />
        <path d="M 10 1 Q 12 2 13 4" stroke="#3a7245" strokeWidth="1.4" fill="none" strokeLinecap="round" />
      </svg>
    )
    case 'meadow': return (
      <svg width="14" height="14" viewBox="0 0 14 14">
        <rect x="0" y="8" width="14" height="6" fill="#6db168" />
        <ellipse cx="7" cy="6" rx="4" ry="3" fill="#4a8a4f" />
        <circle cx="3" cy="3" r="1" fill="#ffd166" />
        <circle cx="11" cy="4" r="1" fill="#ef476f" />
      </svg>
    )
    case 'factory': return (
      <svg width="14" height="14" viewBox="0 0 14 14" shapeRendering="crispEdges">
        <rect x="1" y="6" width="3" height="8" fill="#7a6868" />
        <rect x="5" y="8" width="6" height="6" fill="#5a4d56" />
        <rect x="11" y="4" width="2" height="10" fill="#7a6868" />
        <rect x="6" y="9" width="1" height="2" fill="#ffd166" />
        <rect x="9" y="9" width="1" height="2" fill="#ffd166" />
        <ellipse cx="2.5" cy="4" rx="2" ry="1.5" fill="#dadada" opacity="0.7" />
      </svg>
    )
    default: return null
  }
}

function MoonIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12">
      <path d="M 9 2 A 5 5 0 1 0 10 9.5 A 4 4 0 0 1 9 2 Z" fill="#dadffa" />
    </svg>
  )
}

// ─── Scenery dispatch ─────────────────────────────────────────────────────────

function Decoration({ type }) {
  switch (type) {
    // forest
    case 'pine-large':  return <Pine size={70} />
    case 'pine':        return <Pine size={50} />
    case 'bush':        return <Bush />
    case 'pond':        return <Pond />
    case 'lilypad':     return <Lilypad />
    case 'mushrooms':   return <Mushrooms />
    case 'flower':      return <Flower />
    // beach
    case 'palm-large':  return <Palm size={64} />
    case 'palm':        return <Palm size={46} />
    case 'shell':       return <Shell />
    case 'starfish':    return <Starfish />
    case 'driftwood':   return <Driftwood />
    case 'sand-tuft':   return <SandTuft />
    case 'pebble':      return <Pebble />
    // meadow
    case 'oak':         return <Oak size={1} />
    case 'oak-small':   return <Oak size={0.7} />
    case 'hill-far':    return <HillFar />
    case 'tall-grass':  return <TallGrass />
    case 'wildflower-red':    return <Wildflower color="red" />
    case 'wildflower-yellow': return <Wildflower color="yellow" />
    case 'wildflower-blue':   return <Wildflower color="blue" />
    case 'butterfly':   return <Butterfly />
    // factory
    case 'building':           return <Building variant="default" />
    case 'building-tall':      return <Building variant="tall" />
    case 'smokestack-large':   return <Smokestack size={1.2} />
    case 'smokestack':         return <Smokestack size={0.85} />
    case 'steam':              return <Steam delay={0} />
    case 'steam-b':            return <Steam delay={1.3} />
    case 'steam-c':            return <Steam delay={2.4} />
    case 'gear-deco':          return <GearDeco size={48} reverse={false} />
    case 'gear-deco-small':    return <GearDeco size={32} reverse={true} />
    case 'pipe':               return <Pipe />
    case 'steel-beam':         return <SteelBeam />
    case 'conveyor':           return <Conveyor />
    case 'crate':              return <Crate />
    case 'crate-stack':        return <CrateStack />
    case 'drum-red':           return <Drum color="red" />
    case 'drum-blue':          return <Drum color="blue" />
    case 'drum-yellow':        return <Drum color="yellow" />
    case 'lamp-post':          return <LampPost />
    default:            return null
  }
}

// ─── Forest ───────────────────────────────────────────────────────────────────

function Pine({ size = 50 }) {
  return (
    <svg width={size} height={size * 1.5} viewBox="0 0 16 24" shapeRendering="crispEdges">
      <rect x="7" y="18" width="2" height="6" fill="#3a2a18" />
      <polygon points="8,1 2,8 14,8"   fill="#1f4a2a" />
      <polygon points="8,5 2,13 14,13" fill="#2c5d36" />
      <polygon points="8,9 1,18 15,18" fill="#3a7245" />
      <rect x="6" y="3"  width="1" height="1" fill="#5d9a5d" />
      <rect x="5" y="9"  width="1" height="1" fill="#5d9a5d" />
      <rect x="4" y="14" width="1" height="1" fill="#5d9a5d" />
    </svg>
  )
}

function Bush() {
  return (
    <svg width="44" height="22" viewBox="0 0 22 11" shapeRendering="crispEdges">
      <ellipse cx="6"  cy="7" rx="5" ry="4" fill="#2c5d36" />
      <ellipse cx="15" cy="6" rx="5" ry="4" fill="#3a7245" />
      <ellipse cx="11" cy="5" rx="4" ry="3" fill="#4a8d4f" />
      <rect x="4"  y="4" width="1" height="1" fill="#6db168" />
      <rect x="13" y="3" width="1" height="1" fill="#6db168" />
    </svg>
  )
}

function Pond() {
  return (
    <svg width="180" height="80" viewBox="0 0 90 40">
      <ellipse cx="45" cy="22" rx="42" ry="17" fill="#0f2540" opacity="0.85" />
      <ellipse cx="45" cy="20" rx="36" ry="13" fill="#1d4a72" opacity="0.85" />
      <ellipse cx="45" cy="18" rx="24" ry="6"  fill="#5d8db5" opacity="0.45" />
      <path d="M 16 18 Q 28 14 40 18" stroke="rgba(255,255,255,0.45)" fill="none" strokeWidth="0.6" />
      <path d="M 50 24 Q 62 20 74 24" stroke="rgba(255,255,255,0.30)" fill="none" strokeWidth="0.6" />
    </svg>
  )
}

function Lilypad() {
  return (
    <svg width="20" height="14" viewBox="0 0 20 14">
      <ellipse cx="10" cy="7" rx="9" ry="5" fill="#3a7245" />
      <path d="M 10 7 L 19 7" stroke="#1f4a2a" strokeWidth="0.8" />
      <circle cx="13" cy="6" r="1" fill="#ff8fa3" />
    </svg>
  )
}

function Mushrooms() {
  return (
    <svg width="46" height="30" viewBox="0 0 30 20" shapeRendering="crispEdges">
      <ellipse cx="9"  cy="9"  rx="6" ry="4" fill="#c14747" />
      <circle  cx="6"  cy="8"  r="1" fill="#f5e9d3" />
      <circle  cx="11" cy="9"  r="1" fill="#f5e9d3" />
      <rect    x="7"  y="12" width="4" height="5" fill="#f5e9d3" />
      <ellipse cx="22" cy="13" rx="4" ry="3" fill="#d97070" />
      <circle  cx="20" cy="12" r="0.8" fill="#f5e9d3" />
      <rect    x="20" y="15" width="3" height="4" fill="#f5e9d3" />
    </svg>
  )
}

function Flower() {
  return (
    <svg width="14" height="14" viewBox="0 0 8 8" shapeRendering="crispEdges">
      <circle cx="4" cy="4" r="1.4" fill="#ffd166" />
      <circle cx="2" cy="3" r="1"   fill="#ff6b9d" />
      <circle cx="6" cy="3" r="1"   fill="#ff6b9d" />
      <circle cx="2" cy="5" r="1"   fill="#ff6b9d" />
      <circle cx="6" cy="5" r="1"   fill="#ff6b9d" />
    </svg>
  )
}

// ─── Beach ────────────────────────────────────────────────────────────────────

function Palm({ size = 46 }) {
  const w = size, h = size * 1.7
  return (
    <svg width={w} height={h} viewBox="0 0 26 44">
      {/* curved trunk */}
      <path d="M 13 44 Q 14 30 16 18 Q 17 10 14 6"
            stroke="#7a5630" strokeWidth="2.6" fill="none" strokeLinecap="round" />
      {/* fronds */}
      <path d="M 14 6 Q 6 5 1 1"   stroke="#3a7245" strokeWidth="2.4" fill="none" strokeLinecap="round" />
      <path d="M 14 6 Q 22 5 25 1" stroke="#3a7245" strokeWidth="2.4" fill="none" strokeLinecap="round" />
      <path d="M 14 6 Q 7 10 1 13" stroke="#4a8d4f" strokeWidth="2.4" fill="none" strokeLinecap="round" />
      <path d="M 14 6 Q 21 10 25 13" stroke="#4a8d4f" strokeWidth="2.4" fill="none" strokeLinecap="round" />
      <path d="M 14 6 Q 11 1 9 -2" stroke="#5d9a5d" strokeWidth="2.0" fill="none" strokeLinecap="round" />
      <path d="M 14 6 Q 17 1 19 -2" stroke="#5d9a5d" strokeWidth="2.0" fill="none" strokeLinecap="round" />
      {/* coconuts */}
      <circle cx="12" cy="8" r="1.4" fill="#4a3018" />
      <circle cx="15" cy="9" r="1.4" fill="#4a3018" />
    </svg>
  )
}

function Shell() {
  return (
    <svg width="22" height="18" viewBox="0 0 22 18">
      <path d="M 11 16 Q 2 16 4 5 Q 11 -1 18 5 Q 20 16 11 16 Z"
            fill="#f7d4b5" stroke="#b67a5a" strokeWidth="0.6" />
      <path d="M 11 16 L 8 5"  stroke="#b67a5a" strokeWidth="0.5" />
      <path d="M 11 16 L 14 5" stroke="#b67a5a" strokeWidth="0.5" />
      <path d="M 11 16 L 6  7" stroke="#b67a5a" strokeWidth="0.4" />
      <path d="M 11 16 L 16 7" stroke="#b67a5a" strokeWidth="0.4" />
    </svg>
  )
}

function Starfish() {
  return (
    <svg width="24" height="22" viewBox="0 0 24 22">
      <polygon points="12,2 14,8 20,8 15,12 17,18 12,15 7,18 9,12 4,8 10,8"
               fill="#ff8866" stroke="#c95d3a" strokeWidth="0.6" />
      <circle cx="12" cy="11" r="0.8" fill="#c95d3a" />
      <circle cx="10.5" cy="9.5" r="0.5" fill="#fff" opacity="0.7" />
    </svg>
  )
}

function Driftwood() {
  return (
    <svg width="44" height="14" viewBox="0 0 44 14">
      <ellipse cx="22" cy="9" rx="20" ry="3" fill="#7a5a3a" />
      <ellipse cx="22" cy="8" rx="17" ry="2" fill="#a88a5d" />
      <line x1="6" y1="9" x2="38" y2="9" stroke="#4a3018" strokeWidth="0.5" />
      <line x1="10" y1="7" x2="14" y2="7" stroke="#4a3018" strokeWidth="0.4" />
      <line x1="26" y1="10" x2="32" y2="10" stroke="#4a3018" strokeWidth="0.4" />
    </svg>
  )
}

function SandTuft() {
  return (
    <svg width="22" height="16" viewBox="0 0 22 16">
      <path d="M 3 16 Q 5 6 6 0"   stroke="#9aaa4a" strokeWidth="1" fill="none" strokeLinecap="round" />
      <path d="M 9 16 Q 10 7 11 1" stroke="#b8c45a" strokeWidth="1" fill="none" strokeLinecap="round" />
      <path d="M 15 16 Q 16 6 18 0" stroke="#9aaa4a" strokeWidth="1" fill="none" strokeLinecap="round" />
    </svg>
  )
}

function Pebble() {
  return (
    <svg width="18" height="10" viewBox="0 0 18 10">
      <ellipse cx="6" cy="6" rx="4" ry="2.5" fill="#a89d8a" />
      <ellipse cx="12" cy="7" rx="3" ry="2"  fill="#8a8074" />
      <ellipse cx="5" cy="5" rx="1.5" ry="0.8" fill="#c4baa8" opacity="0.7" />
    </svg>
  )
}

// ─── Meadow ───────────────────────────────────────────────────────────────────

function Oak({ size = 1 }) {
  const w = 96 * size, h = 110 * size
  return (
    <svg width={w} height={h} viewBox="0 0 32 36" shapeRendering="auto">
      <rect x="14" y="22" width="4" height="14" fill="#5a3a18" />
      <rect x="13" y="28" width="1" height="6"  fill="#3a2a18" />
      <ellipse cx="16" cy="14" rx="14" ry="11" fill="#3a7245" />
      <ellipse cx="9"  cy="11" rx="6"  ry="5"  fill="#4a8a4f" />
      <ellipse cx="22" cy="13" rx="6"  ry="5"  fill="#4a8a4f" />
      <ellipse cx="16" cy="8"  rx="6"  ry="4"  fill="#5d9a5d" />
      <ellipse cx="11" cy="14" rx="2"  ry="1.5" fill="#2c5d36" opacity="0.7" />
    </svg>
  )
}

function HillFar() {
  return (
    <svg width="280" height="90" viewBox="0 0 140 45" preserveAspectRatio="none">
      <ellipse cx="70" cy="55" rx="70" ry="22" fill="#3a6f4a" />
      <ellipse cx="70" cy="52" rx="60" ry="16" fill="#4a8a4f" opacity="0.6" />
    </svg>
  )
}

function TallGrass() {
  return (
    <svg width="42" height="22" viewBox="0 0 42 22">
      <path d="M 4 22 Q 5 8 7 0"     stroke="#3a7245" strokeWidth="1.1" fill="none" strokeLinecap="round" />
      <path d="M 11 22 Q 12 12 14 4" stroke="#4a8a4f" strokeWidth="1.1" fill="none" strokeLinecap="round" />
      <path d="M 20 22 Q 21 6 24 -1" stroke="#3a7245" strokeWidth="1.1" fill="none" strokeLinecap="round" />
      <path d="M 30 22 Q 31 10 34 2" stroke="#4a8a4f" strokeWidth="1.1" fill="none" strokeLinecap="round" />
      <path d="M 38 22 Q 39 14 41 6" stroke="#3a7245" strokeWidth="1.1" fill="none" strokeLinecap="round" />
    </svg>
  )
}

function Wildflower({ color = 'red' }) {
  const colors = { red: '#ef476f', yellow: '#ffd166', blue: '#7aa9ff' }
  const c = colors[color] || colors.red
  return (
    <svg width="14" height="22" viewBox="0 0 14 22">
      <line x1="7" y1="22" x2="7" y2="9" stroke="#3a7245" strokeWidth="1" />
      <ellipse cx="3" cy="15" rx="3" ry="1.4" fill="#4a8a4f" />
      <circle cx="7" cy="6" r="3" fill={c} />
      <circle cx="7" cy="6" r="1" fill="#ffe9a3" />
    </svg>
  )
}

function Butterfly() {
  return (
    <div className="butterfly-anim">
      <svg width="22" height="16" viewBox="0 0 22 16">
        <ellipse cx="11" cy="8" rx="0.8" ry="3" fill="#1a1722" />
        <path d="M 11 6 Q 4 1 1 6 Q 4 11 11 8 Z"  fill="#ff6b9d" stroke="#1a1722" strokeWidth="0.4" />
        <path d="M 11 6 Q 18 1 21 6 Q 18 11 11 8 Z" fill="#ff6b9d" stroke="#1a1722" strokeWidth="0.4" />
        <path d="M 11 8 Q 5 13 4 11 Q 6 15 11 12 Z"  fill="#ff8fa3" stroke="#1a1722" strokeWidth="0.4" />
        <path d="M 11 8 Q 17 13 18 11 Q 16 15 11 12 Z" fill="#ff8fa3" stroke="#1a1722" strokeWidth="0.4" />
      </svg>
    </div>
  )
}

// ─── Factory ──────────────────────────────────────────────────────────────────

function Building({ variant = 'default' }) {
  // Squat factory building with a sawtooth roof and lit windows.
  const tall = variant === 'tall'
  const w = tall ? 200 : 180
  const h = tall ? 90  : 70
  return (
    <svg width={w} height={h} viewBox={`0 0 ${tall ? 100 : 90} ${tall ? 45 : 35}`} shapeRendering="crispEdges">
      {/* main wall */}
      <rect x="2" y={tall ? 12 : 8} width={tall ? 96 : 86} height={tall ? 32 : 26} fill="#3a323e" />
      <rect x="2" y={tall ? 12 : 8} width={tall ? 96 : 86} height="2" fill="#5a4d56" />
      {/* sawtooth roof */}
      {Array.from({ length: tall ? 6 : 5 }).map((_, i) => {
        const x = 2 + i * (tall ? 16 : 17)
        const y = tall ? 12 : 8
        return (
          <polygon key={i}
            points={`${x},${y} ${x + (tall ? 8 : 8.5)},${y - 8} ${x + (tall ? 16 : 17)},${y}`}
            fill="#4a414e" />
        )
      })}
      {/* lit windows */}
      {Array.from({ length: tall ? 7 : 6 }).map((_, i) => {
        const x = 6 + i * (tall ? 13 : 14)
        const y = tall ? 22 : 18
        return (
          <rect key={`w-${i}`} x={x} y={y} width="4" height="6" fill="#ffd166" opacity="0.85" />
        )
      })}
    </svg>
  )
}

function Smokestack({ size = 1 }) {
  const w = 32 * size, h = 110 * size
  return (
    <svg width={w} height={h} viewBox="0 0 16 55" shapeRendering="crispEdges">
      {/* base flange */}
      <rect x="2" y="50" width="12" height="5" fill="#4a3838" />
      {/* tower */}
      <rect x="4"  y="6"  width="8" height="44" fill="#7a6868" />
      <rect x="4"  y="6"  width="2" height="44" fill="#9a8a8a" />
      <rect x="10" y="6"  width="2" height="44" fill="#5a4848" />
      {/* bands */}
      <rect x="3" y="14" width="10" height="1" fill="#3a2828" />
      <rect x="3" y="26" width="10" height="1" fill="#3a2828" />
      <rect x="3" y="38" width="10" height="1" fill="#3a2828" />
      {/* top rim */}
      <rect x="3" y="3" width="10" height="3" fill="#5a4848" />
      <rect x="5" y="2" width="6"  height="2" fill="#1a0808" />
    </svg>
  )
}

function Steam({ delay = 0 }) {
  return (
    <div className="steam-anim" style={{ animationDelay: `${delay}s` }}>
      <svg width="44" height="60" viewBox="0 0 22 30">
        <ellipse cx="9"  cy="24" rx="6" ry="5" fill="#dadada" opacity="0.55" />
        <ellipse cx="14" cy="17" rx="7" ry="5" fill="#e8e8e8" opacity="0.7"  />
        <ellipse cx="9"  cy="9"  rx="6" ry="5" fill="#f0f0f0" opacity="0.8"  />
        <ellipse cx="14" cy="3"  rx="5" ry="4" fill="#ffffff" opacity="0.5"  />
      </svg>
    </div>
  )
}

function GearDeco({ size = 40, reverse = false }) {
  const teeth = [0, 45, 90, 135, 180, 225, 270, 315]
  return (
    <div className={reverse ? 'gear-anim-reverse' : 'gear-anim'}>
      <svg width={size} height={size} viewBox="-12 -12 24 24" shapeRendering="auto">
        {teeth.map(a => (
          <rect key={a} x="-2.2" y="-12" width="4.4" height="4" fill="#7a6868"
                transform={`rotate(${a})`} />
        ))}
        <circle r="9"   fill="#5a4848" />
        <circle r="7.5" fill="#9a8a8a" />
        <circle r="5"   fill="#7a6868" />
        <circle r="2"   fill="#3a2828" />
      </svg>
    </div>
  )
}

function Pipe() {
  return (
    <svg width="220" height="20" viewBox="0 0 110 10" shapeRendering="crispEdges">
      <rect x="0" y="2" width="110" height="6" fill="#7a6868" />
      <rect x="0" y="2" width="110" height="1" fill="#9a8a8a" />
      <rect x="0" y="7" width="110" height="1" fill="#5a4848" />
      {/* flanges */}
      <rect x="22" y="0"  width="3" height="10" fill="#5a4848" />
      <rect x="55" y="0"  width="3" height="10" fill="#5a4848" />
      <rect x="88" y="0"  width="3" height="10" fill="#5a4848" />
    </svg>
  )
}

function SteelBeam() {
  return (
    <svg width="140" height="46" viewBox="0 0 70 23">
      <line x1="2"  y1="2"  x2="68" y2="21" stroke="#5a4848" strokeWidth="2" />
      <line x1="68" y1="2"  x2="2"  y2="21" stroke="#5a4848" strokeWidth="2" />
      <line x1="2"  y1="2"  x2="68" y2="2"  stroke="#7a6868" strokeWidth="2" />
      <line x1="2"  y1="21" x2="68" y2="21" stroke="#7a6868" strokeWidth="2" />
      {[2, 68].flatMap(x => [
        <circle key={`t-${x}`} cx={x} cy="2"  r="1" fill="#3a2828" />,
        <circle key={`b-${x}`} cx={x} cy="21" r="1" fill="#3a2828" />,
      ])}
    </svg>
  )
}

function Conveyor() {
  return (
    <svg width="100" height="30" viewBox="0 0 50 15" shapeRendering="crispEdges">
      {/* legs */}
      <rect x="6"  y="10" width="2" height="5" fill="#3a2828" />
      <rect x="42" y="10" width="2" height="5" fill="#3a2828" />
      {/* belt frame */}
      <rect x="3" y="3" width="44" height="7" fill="#3a3340" />
      <rect x="3" y="3" width="44" height="1" fill="#5a5060" />
      {/* rollers */}
      <circle cx="5"  cy="6.5" r="3" fill="#7a6868" />
      <circle cx="45" cy="6.5" r="3" fill="#7a6868" />
      <circle cx="5"  cy="6.5" r="1" fill="#3a2828" />
      <circle cx="45" cy="6.5" r="1" fill="#3a2828" />
      {/* belt surface */}
      <line x1="8" y1="6.5" x2="42" y2="6.5" stroke="#5a5060" strokeWidth="0.6" />
      {/* a small crate riding the belt */}
      <rect x="22" y="2" width="6" height="4" fill="#8a6a3a" />
      <rect x="22" y="2" width="6" height="1" fill="#a88a4a" />
    </svg>
  )
}

function Crate() {
  return (
    <svg width="36" height="32" viewBox="0 0 18 16" shapeRendering="crispEdges">
      <rect x="1" y="2"  width="16" height="13" fill="#8a6a3a" />
      <rect x="1" y="2"  width="16" height="2"  fill="#a88a4a" />
      <rect x="1" y="13" width="16" height="2"  fill="#5a4220" />
      <line x1="9" y1="2" x2="9" y2="15" stroke="#5a4220" strokeWidth="0.6" />
      <rect x="1"  y="2" width="2" height="13" fill="#3a2818" opacity="0.3" />
      <rect x="15" y="2" width="2" height="13" fill="#3a2818" opacity="0.3" />
    </svg>
  )
}

function CrateStack() {
  return (
    <svg width="44" height="56" viewBox="0 0 22 28" shapeRendering="crispEdges">
      {/* bottom crate */}
      <rect x="1" y="14" width="20" height="13" fill="#8a6a3a" />
      <rect x="1" y="14" width="20" height="2"  fill="#a88a4a" />
      <line x1="11" y1="14" x2="11" y2="27" stroke="#5a4220" strokeWidth="0.6" />
      {/* top crate, slightly off-center */}
      <rect x="4" y="2"  width="14" height="11" fill="#7d5e2e" />
      <rect x="4" y="2"  width="14" height="2"  fill="#a08a4a" />
      <line x1="11" y1="2" x2="11" y2="13" stroke="#5a4220" strokeWidth="0.6" />
    </svg>
  )
}

function Drum({ color = 'red' }) {
  const colors = { red: '#c14747', blue: '#3a6f8a', yellow: '#d9a72e' }
  const c = colors[color] || colors.red
  return (
    <svg width="24" height="34" viewBox="0 0 12 18" shapeRendering="crispEdges">
      <rect x="2" y="2" width="8" height="14" fill={c} />
      <rect x="2" y="2" width="1" height="14" fill="#fff" opacity="0.30" />
      <rect x="9" y="2" width="1" height="14" fill="#000" opacity="0.30" />
      <rect x="2" y="2"  width="8" height="1" fill="#000" opacity="0.40" />
      <rect x="2" y="15" width="8" height="1" fill="#000" opacity="0.40" />
      <rect x="2" y="8"  width="8" height="1" fill="#000" opacity="0.30" />
      {/* hazard label */}
      <rect x="4" y="10" width="4" height="2" fill="#1a1722" opacity="0.35" />
    </svg>
  )
}

function LampPost() {
  return (
    <svg width="22" height="64" viewBox="0 0 11 32" shapeRendering="crispEdges">
      {/* base */}
      <rect x="3" y="29" width="5" height="3"  fill="#3a3340" />
      {/* post */}
      <rect x="5" y="6"  width="1" height="23" fill="#3a3340" />
      {/* arm */}
      <rect x="2" y="5"  width="7" height="1"  fill="#3a3340" />
      {/* lamp head */}
      <rect x="1" y="3"  width="9" height="3"  fill="#5a5060" />
      <rect x="2" y="2"  width="7" height="1"  fill="#7a6868" />
      {/* glowing bulb */}
      <ellipse cx="5.5" cy="5" rx="3" ry="1.6" fill="#ffd166" />
      <ellipse cx="5.5" cy="5" rx="2" ry="1"   fill="#fff8d8" />
    </svg>
  )
}

// ─── Zone landmarks ────────────────────────────────────────────────────────

function Landmark({ project }) {
  switch (project) {
    case 'north': return <TreesLandmark />
    case 'east':  return <BoardLandmark />
    case 'west':  return <CompassLandmark />
    case 'south': return <ChartLandmark />
    default:       return null
  }
}

function TreesLandmark() {
  return (
    <svg width="92" height="92" viewBox="0 0 46 46" shapeRendering="crispEdges">
      <rect x="13" y="22" width="2" height="14" fill="#3a2a18" />
      <rect x="31" y="22" width="2" height="14" fill="#3a2a18" />
      <polygon points="14,4 8,14 20,14"   fill="#7aa9ff" />
      <polygon points="14,9 6,22 22,22"   fill="#9fc0ff" />
      <polygon points="32,4 26,14 38,14"  fill="#ef476f" />
      <polygon points="32,9 24,22 40,22"  fill="#ff7e9b" />
      <rect x="17" y="33" width="12" height="2" fill="#7a5a32" />
      <rect x="18" y="35" width="2"  height="3" fill="#7a5a32" />
      <rect x="26" y="35" width="2"  height="3" fill="#7a5a32" />
    </svg>
  )
}

function BoardLandmark() {
  return (
    <svg width="76" height="76" viewBox="0 0 38 38" shapeRendering="crispEdges">
      <rect x="6" y="4" width="26" height="18" fill="#1a3320" />
      <rect x="6" y="4" width="26" height="18" fill="none" stroke="#7a5a32" strokeWidth="2" />
      <text x="19" y="17" textAnchor="middle"
            fontFamily="'SF Mono', Menlo, monospace" fontSize="9" fontWeight="700"
            fill="#ffd166">ABC</text>
      <line x1="10" y1="22" x2="6"  y2="34" stroke="#7a5a32" strokeWidth="2" />
      <line x1="28" y1="22" x2="32" y2="34" stroke="#7a5a32" strokeWidth="2" />
      <rect x="5" y="33" width="28" height="2" fill="#7a5a32" />
    </svg>
  )
}

function CompassLandmark() {
  return (
    <svg width="68" height="92" viewBox="0 0 34 46" shapeRendering="crispEdges">
      <polygon points="17,4 12,11 12,42 22,42 22,11" fill="#5a4d6a" />
      <polygon points="17,4 12,11 22,11"             fill="#7a6d8a" />
      <rect x="11" y="42" width="12" height="2" fill="#3a334a" />
      <circle cx="17" cy="24" r="4.5" fill="none" stroke="#c084fc" strokeWidth="1.4" />
      <circle cx="17" cy="24" r="1.6" fill="#c084fc" />
      <line x1="17" y1="17" x2="17" y2="19" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="17" y1="29" x2="17" y2="31" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="10" y1="24" x2="12" y2="24" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="22" y1="24" x2="24" y2="24" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="12" y1="19" x2="14" y2="21" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="20" y1="27" x2="22" y2="29" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="22" y1="19" x2="20" y2="21" stroke="#c084fc" strokeWidth="1.4" />
      <line x1="14" y1="27" x2="12" y2="29" stroke="#c084fc" strokeWidth="1.4" />
    </svg>
  )
}

function ChartLandmark() {
  return (
    <svg width="68" height="92" viewBox="0 0 34 46" shapeRendering="crispEdges">
      <polygon points="17,4 12,11 12,42 22,42 22,11" fill="#3a5d4a" />
      <polygon points="17,4 12,11 22,11"             fill="#5a8d6a" />
      <rect x="11" y="42" width="12" height="2" fill="#234032" />
      <rect x="13" y="26" width="2"  height="6"  fill="#5dd1a6" />
      <rect x="16" y="22" width="2"  height="10" fill="#5dd1a6" />
      <rect x="19" y="24" width="2"  height="8"  fill="#5dd1a6" />
      <line x1="12" y1="33" x2="22" y2="33" stroke="#5dd1a6" strokeWidth="0.8" />
    </svg>
  )
}
