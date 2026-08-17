<template>
  <main class="app-shell">
    <aside class="sidebar">
      <section class="brand">
        <div class="brand-mark">EM</div>
        <div>
          <h1>RelayDesk 企业统一服务台</h1>
          <p>多 Agent 企业服务请求协同</p>
        </div>
      </section>

      <a class="profile-card" href="https://xhslink.com/m/558VOQs4Otc" target="_blank" rel="noreferrer">
        <span>我的主页</span>
        <strong>小红书 69.6K 次赞与收藏</strong>
        <em>来看看我的主页 &gt;&gt;</em>
      </a>

      <section class="panel">
        <div class="panel-heading">
          <h2>后端</h2>
          <span class="pill">{{ currentBackend.label }}</span>
        </div>
        <div class="segmented">
          <button :class="{ active: settings.backend === 'java' }" @click="switchBackend('java')">Java</button>
          <button :class="{ active: settings.backend === 'python' }" @click="switchBackend('python')">Python</button>
        </div>

        <label>
          <span>Java API</span>
          <input v-model="settings.endpoints.java" @change="persist" placeholder="/api/java" />
        </label>
        <label>
          <span>Python API</span>
          <input v-model="settings.endpoints.python" @change="persist" placeholder="/api/python" />
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="settings.userId" @change="persist" placeholder="u1001" />
        </label>
        <label>
          <span>会话 ID</span>
          <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
        </label>

        <div class="actions">
          <button @click="checkHealth">健康检查</button>
          <button @click="loadStats">刷新状态</button>
        </div>
      </section>

      <section class="panel status-panel">
        <div class="panel-heading">
          <h2>状态</h2>
          <span :class="['status-dot', healthOk ? 'online' : 'offline']"></span>
        </div>
        <dl>
          <div>
            <dt>当前后端</dt>
            <dd>{{ currentBackend.label }}</dd>
          </div>
          <div>
            <dt>健康状态</dt>
            <dd :class="healthOk ? 'ok' : 'muted'">{{ healthLabel }}</dd>
          </div>
          <div>
            <dt>知识片段</dt>
            <dd>{{ knowledgeCount }}</dd>
          </div>
        </dl>
        <pre v-if="statusText">{{ statusText }}</pre>
      </section>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div>
          <span class="eyebrow">RelayDesk Service Desk</span>
          <h2>企业服务对话</h2>
          <p>{{ currentBackend.baseUrl }}</p>
        </div>
        <div class="header-actions">
          <a class="profile-link" href="https://xhslink.com/m/558VOQs4Otc" target="_blank" rel="noreferrer">小红书主页</a>
          <a :href="docsUrl" target="_blank" rel="noreferrer">API 文档</a>
        </div>
      </header>

      <section class="chat-panel">
        <div class="messages" ref="messageList">
          <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
            <div class="message-meta">
              <span>{{ item.role === 'user' ? '用户' : currentBackend.label }}</span>
              <small v-if="item.meta">{{ item.meta }}</small>
            </div>
            <p>{{ item.content }}</p>
          </article>
          <div v-if="messages.length === 0" class="empty-state">
            <h3>欢迎使用 RelayDesk 企业统一服务台</h3>
            <p>你可以咨询通用服务、账户问题、技术故障、费用结算或申请人工协助。</p>
            <div class="quick-prompts">
              <button v-for="item in exampleQuestions" :key="item" type="button" @click="draft = item">{{ item }}</button>
            </div>
          </div>
        </div>

        <form class="composer" @submit.prevent="sendMessage">
          <textarea v-model="draft" rows="3" placeholder="输入问题，例如：忘记企业账号密码怎么办？"></textarea>
          <button :disabled="busy || !draft.trim()">{{ busy ? '发送中' : '发送' }}</button>
        </form>
      </section>

      <section class="tools-grid">
        <article class="tool-panel">
          <div class="panel-heading">
            <h2>知识库检索</h2>
            <span class="pill soft">RAG</span>
          </div>
          <div class="inline-form">
            <input v-model="searchQuery" placeholder="退款多久能到账" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">检索</button>
          </div>
          <div class="result-list">
            <article v-for="item in searchResults" :key="item.id || item.title" class="result-item">
              <strong>{{ item.title || '未命名结果' }}</strong>
              <span>score {{ item.score ?? '-' }}</span>
              <p>{{ item.content }}</p>
            </article>
          </div>
        </article>

        <article class="tool-panel">
          <div class="panel-heading">
            <h2>导入演示知识</h2>
            <span class="pill soft">Demo Docs</span>
          </div>
          <label>
            <span>标题</span>
            <input v-model="docTitle" placeholder="企业服务补充说明（演示）" />
          </label>
          <label>
            <span>内容</span>
            <textarea v-model="docContent" rows="5" placeholder="输入适用场景、处理步骤和人工升级条件；请勿冒充真实企业制度"></textarea>
          </label>
          <div class="actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
            <label class="file-button">
              上传文件
              <input type="file" accept=".txt,.md,.json" @change="handleUpload" />
            </label>
          </div>
        </article>
      </section>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  requestChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  saveSettings,
  uploadKnowledge
} from './lib/backends'

const settings = reactive(createInitialSettings())
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('退款多久能到账')
const searchResults = ref([])
const docTitle = ref('企业服务补充说明（演示）')
const docContent = ref('适用场景：员工咨询通用服务流程。处理步骤：说明需求并提供必要的脱敏信息。人工升级条件：涉及真实审批、后台权限或高风险操作。此内容仅为演示制度。')
const messageList = ref(null)
const exampleQuestions = [
  '忘记企业账号密码怎么办？',
  '登录一直报401。',
  '系统页面出现500错误。',
  '如何修改发票抬头？',
  '为什么被重复扣款？',
  '这个问题比较紧急，请帮我转人工。'
]

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => {
  if (settings.backend === 'java') return `${currentBackend.value.baseUrl}/docs`
  return `${currentBackend.value.baseUrl}/docs`
})

watch(
  () => settings.conversationId,
  () => persist()
)

onMounted(() => {
  checkHealth()
  loadStats()
})

function switchBackend(type) {
  settings.backend = type
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  statusText.value = ''
  searchResults.value = []
  checkHealth()
}

function persist() {
  saveSettings(settings)
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content) return
  messages.value.push({ id: crypto.randomUUID(), role: 'user', content })
  draft.value = ''
  busy.value = true
  try {
    const response = await requestChat(settings.backend, settings, content)
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    const meta = [
      response.intent,
      displayAgentName(response.agentType),
      response.knowledgeUsed ? 'RAG' : '',
      response.escalated ? '转人工' : ''
    ].filter(Boolean).join(' · ')
    messages.value.push({
      id: crypto.randomUUID(),
      role: 'assistant',
      content: response.response,
      meta
    })
  } catch (error) {
    messages.value.push({
      id: crypto.randomUUID(),
      role: 'assistant',
      content: error.message,
      meta: '请求失败'
    })
  } finally {
    busy.value = false
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

function displayAgentName(type) {
  return {
    general: '综合服务协调Agent',
    technical: '技术支持Agent',
    billing: '费用与结算Agent',
    escalation: '人工升级通道'
  }[type] || type
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings.backend, settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function loadStats() {
  try {
    const [stats, monitor] = await Promise.allSettled([
      requestKnowledgeStats(settings.backend, settings),
      requestMonitor(settings.backend, settings)
    ])
    if (stats.status === 'fulfilled') {
      knowledgeCount.value = stats.value.total_chunks ?? stats.value.totalChunks ?? '-'
    }
    if (monitor.status === 'fulfilled') {
      statusText.value = JSON.stringify(monitor.value, null, 2)
    }
  } catch (error) {
    statusText.value = error.message
  }
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings.backend, settings, searchQuery.value, 5)
    searchResults.value = data.results || []
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function submitKnowledge() {
  busy.value = true
  try {
    const data = await addKnowledge(settings.backend, settings, [
      { title: docTitle.value.trim(), content: docContent.value.trim() }
    ])
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const data = await uploadKnowledge(settings.backend, settings, file)
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}
</script>
