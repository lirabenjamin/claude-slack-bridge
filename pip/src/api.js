const TOKEN_KEY = 'pip-token'

;(function captureTokenFromUrl() {
  if (typeof window === 'undefined') return
  const url = new URL(window.location.href)
  const t = url.searchParams.get('t')
  if (t) {
    localStorage.setItem(TOKEN_KEY, t)
    url.searchParams.delete('t')
    window.history.replaceState({}, '', url.toString())
  }
})()

function authHeaders(extra = {}) {
  const t = localStorage.getItem(TOKEN_KEY)
  const h = { ...extra }
  if (t) h['Authorization'] = `Bearer ${t}`
  return h
}

async function checkAuth(r) {
  if (r.status === 401) {
    throw new Error('unauthorized — open the URL with ?t=<token>')
  }
}

export async function fetchThreads() {
  const r = await fetch('/api/threads', { headers: authHeaders() })
  await checkAuth(r)
  if (!r.ok) throw new Error(`threads ${r.status}`)
  return r.json()
}

export async function fetchThread(threadKey) {
  const r = await fetch(`/api/thread/${encodeURIComponent(threadKey)}`, {
    headers: authHeaders(),
  })
  await checkAuth(r)
  if (!r.ok) throw new Error(`thread ${r.status}`)
  return r.json()
}

export async function postReply(threadKey, text) {
  const r = await fetch(`/api/thread/${encodeURIComponent(threadKey)}/reply`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ text }),
  })
  await checkAuth(r)
  if (!r.ok) throw new Error(`reply ${r.status}`)
  return r.json()
}

export async function updateSprite(threadKey, updates) {
  const r = await fetch(`/api/thread/${encodeURIComponent(threadKey)}/sprite`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(updates),
  })
  await checkAuth(r)
  if (!r.ok) throw new Error(`sprite ${r.status}`)
  return r.json()
}

export async function forgetThread(threadKey) {
  const r = await fetch(`/api/thread/${encodeURIComponent(threadKey)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  await checkAuth(r)
  if (!r.ok) throw new Error(`forget ${r.status}`)
  return r.json()
}
