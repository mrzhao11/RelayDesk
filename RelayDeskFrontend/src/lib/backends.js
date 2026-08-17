const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: import.meta.env.VITE_PYTHON_API_URL || '/api/python',
    port: '8000'
  },
  java: {
    id: 'java',
    label: 'Java',
    baseUrl: import.meta.env.VITE_JAVA_API_URL || '/api/java',
    port: '8080'
  }
}

export function createInitialSettings() {
  const saved = readSettings()
  return {
    settingsVersion: 2,
    backend: saved.settingsVersion === 2 ? (saved.backend || 'python') : 'python',
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    adminKey: '',
    endpoints: {
      python: saved.endpoints?.python || DEFAULT_BACKENDS.python.baseUrl,
      java: saved.endpoints?.java || DEFAULT_BACKENDS.java.baseUrl
    }
  }
}

export function saveSettings(settings) {
  const { adminKey: _adminKey, ...safeSettings } = settings
  localStorage.setItem('relaydesk.frontend.settings', JSON.stringify(safeSettings))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.java
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/health')
}

export async function requestMonitor(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/monitor')
}

export async function requestKnowledgeStats(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/stats')
}

export async function requestSearch(type, settings, query, topK = 5) {
  const params = new URLSearchParams({
    query,
    [type === 'python' ? 'top_k' : 'topK']: String(topK)
  })
  return requestJson(backendMeta(type, settings).baseUrl, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const raw = await requestJson(meta.baseUrl, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  return normalizeChatResponse(type, raw)
}

export async function addKnowledge(type, settings, documents) {
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/add', {
    method: 'POST',
    headers: adminHeaders(settings, { 'Content-Type': 'application/json' }),
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/upload', {
    method: 'POST',
    headers: adminHeaders(settings),
    body: form
  })
}

function adminHeaders(settings, headers = {}) {
  const key = String(settings.adminKey || '').trim()
  return key
    ? { ...headers, 'X-RelayDesk-Admin-Key': key }
    : headers
}

function buildChatPayload(type, settings, message) {
  if (type === 'python') {
    return {
      message,
      user_id: settings.userId || 'anonymous',
      conv_id: settings.conversationId || undefined
    }
  }
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conversation_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    verified: raw.verified,
    grounded: raw.grounded,
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new Error(`${response.status} ${response.statusText}: ${detail}`)
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('relaydesk.frontend.settings') || '{}')
  } catch {
    return {}
  }
}
