import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import Sprite from './Sprite.jsx'
import { fetchThread, postReply, forgetThread, updateSprite } from './api.js'
import { slackToMarkdown } from './slack-md.js'

function fmtTs(ts) {
  if (!ts) return ''
  const d = new Date(parseFloat(ts) * 1000)
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

const MD_COMPONENTS = {
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
}

export default function ThreadPanel({ threadKey, onClose, onForgot, onSpriteChange }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [confirmKill, setConfirmKill] = useState(false)
  const [editingName, setEditingName] = useState(false)
  const [draftName, setDraftName] = useState('')
  const bodyRef = useRef(null)
  const textareaRef = useRef(null)

  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 320) + 'px'
  }, [text])

  async function load() {
    try {
      const d = await fetchThread(threadKey)
      setData(d)
    } catch (e) {
      setError(String(e))
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 4000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadKey])

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [data?.messages?.length])

  async function send() {
    if (!text.trim() || sending) return
    setSending(true)
    try {
      await postReply(threadKey, text.trim())
      setText('')
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setSending(false)
    }
  }

  async function killSprite() {
    try {
      await forgetThread(threadKey)
      onForgot?.()
    } catch (e) {
      setError(String(e))
    }
  }

  async function commitName() {
    const name = draftName.trim()
    setEditingName(false)
    if (!name || name === sprite.name) return
    try {
      await updateSprite(threadKey, { name })
      load()
      onSpriteChange?.()
    } catch (e) {
      setError(String(e))
    }
  }

  async function unpark() {
    try {
      await updateSprite(threadKey, { parked_at: null })
      load()
      onSpriteChange?.()
    } catch (e) {
      setError(String(e))
    }
  }

  const sprite = data?.sprite || {}
  const isParked = !!sprite.parked_at

  return (
    <>
      <div className="panel-backdrop" onClick={onClose} />
      <div className="panel">
        <div className="panel-header">
          {data && <Sprite kind={sprite.kind} hue={sprite.hue} />}
          <div style={{ minWidth: 0 }}>
            {editingName ? (
              <input
                className="name-input"
                value={draftName}
                autoFocus
                maxLength={24}
                onChange={e => setDraftName(e.target.value)}
                onBlur={commitName}
                onKeyDown={e => {
                  if (e.key === 'Enter')  { e.preventDefault(); e.currentTarget.blur() }
                  if (e.key === 'Escape') { setEditingName(false); setDraftName(sprite.name || '') }
                }}
              />
            ) : (
              <button
                className="name name-edit"
                onClick={() => { setDraftName(sprite.name || ''); setEditingName(true) }}
                title="rename"
              >{sprite.name || '…'}</button>
            )}
            <div className="channel">
              {data ? <>#{data.channel_name || 'dm'} · {data.status.replace('_', ' ')}{isParked ? ' · parked' : ''}</> : 'loading'}
            </div>
          </div>
          <div className="header-actions">
            {data?.permalink && (
              <a
                className="kill slack-link"
                href={data.permalink}
                target="_blank"
                rel="noreferrer"
                title="open thread in Slack"
              >slack ↗</a>
            )}
            {data && isParked && (
              <button className="kill" onClick={unpark} title="let it wander again">unpark</button>
            )}
            {data && (
              confirmKill ? (
                <>
                  <button className="kill confirm" onClick={killSprite} title="confirm forget">forget</button>
                  <button className="kill cancel" onClick={() => setConfirmKill(false)}>cancel</button>
                </>
              ) : (
                <button className="kill" onClick={() => setConfirmKill(true)} title="forget this sprite">💀</button>
              )
            )}
            <button className="close" aria-label="close" onClick={onClose}>×</button>
          </div>
        </div>

        <div className="panel-body" ref={bodyRef}>
          {error && <div className="empty">⚠ {error}</div>}
          {!data && !error && <div className="empty"><span className="spinner"/> loading…</div>}

          {data?.messages?.length === 0 && <div className="empty">no messages yet</div>}

          {data?.messages?.map(m => (
            <div key={m.ts} className={`message ${m.is_bot ? 'bot' : 'user'}`}>
              <div className="md">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                  {slackToMarkdown(m.text)}
                </ReactMarkdown>
              </div>
              {m.files?.length > 0 && m.files.map(f => (
                <div key={f.url} className="attachment">
                  📎 <a href={f.url} target="_blank" rel="noreferrer">{f.name}</a>
                </div>
              ))}
              <span className="ts">{fmtTs(m.ts)}</span>
            </div>
          ))}

          {data?.md_paths?.length > 0 && (
            <div className="md-paths">
              <div style={{ marginBottom: 6 }}>📓 referenced files</div>
              {data.md_paths.map(p => (
                <div key={p}><code>{p}</code></div>
              ))}
            </div>
          )}
        </div>

        <div className="reply">
          <textarea
            ref={textareaRef}
            placeholder="reply…"
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send() }
            }}
            rows={2}
          />
          <button onClick={send} disabled={!text.trim() || sending}>
            {sending ? <span className="spinner"/> : 'send'}
          </button>
        </div>
      </div>
    </>
  )
}
