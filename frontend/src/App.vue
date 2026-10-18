<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { Component } from 'vue'
import { contestHealth } from './api/contest.ts'
import { credentials, knowledgeApi, setToken, sql } from './state/workbench.ts'
import HomePage from './pages/HomePage.vue'
import ChatPage from './pages/ChatPage.vue'
import SqlPage from './pages/SqlPage.vue'
import KnowledgePage from './pages/KnowledgePage.vue'
import LibraryPage from './pages/LibraryPage.vue'
import TracePage from './pages/TracePage.vue'
import FusionPage from './pages/FusionPage.vue'

// 复用参考包导航/工作台布局，页面字段与交互改为阶段2真实契约。
const nav = [
  { id: 'home', name: '总览', icon: '◫', group: 'WORKSPACE' },
  { id: 'chat', name: '智能问数与澄清', icon: '✳', group: 'WORKSPACE' },
  { id: 'sql', name: 'NL2SQL 查询凭证', icon: '▤', group: 'WORKSPACE' },
  { id: 'knowledge', name: '文档知识问答', icon: '◈', group: 'WORKSPACE' },
  { id: 'fusion', name: '人工跨源联动', icon: '◇', group: 'WORKSPACE' },
  { id: 'library', name: '知识库管理', icon: '▣', group: 'DATA & OPS' },
  { id: 'trace', name: '执行链路追踪', icon: '⌁', group: 'DATA & OPS' },
]
const views: Record<string, Component> = { home: HomePage, chat: ChatPage, sql: SqlPage,
  knowledge: KnowledgePage, library: LibraryPage, trace: TracePage, fusion: FusionPage }
const current = ref('home')
const menuOpen = ref(false)
const tokenInput = ref('')
const currentMeta = computed(() => nav.find(item => item.id === current.value) || nav[0])
const activeView = computed(() => views[current.value])
type HealthState = 'unchecked' | 'checking' | 'alive' | 'unavailable'
const services = ref<{ contest: HealthState; knowledge: HealthState }>({ contest: 'unchecked', knowledge: 'unchecked' })
const refreshing = ref(false)

/** 导航保留页面内存状态，不把会话或问题写入路由或浏览器存储。 */
function navigate(id: string) {
  if (!views[id]) return
  current.value = id; menuOpen.value = false
}

/** 并行读取两个真实 /health，HTTP失败或坏DTO都显示不可用。 */
async function checkHealth() {
  if (refreshing.value) return
  refreshing.value = true; services.value = { contest: 'checking', knowledge: 'checking' }
  const result = await Promise.allSettled([contestHealth(), knowledgeApi.health()])
  services.value.contest = result[0].status === 'fulfilled' ? 'alive' : 'unavailable'
  services.value.knowledge = result[1].status === 'fulfilled' ? 'alive' : 'unavailable'
  refreshing.value = false
}

/** 入口令牌只在当前标签页内存保存，身份切换立即清除旧SQL会话。 */
function applyToken() { setToken(tokenInput.value); tokenInput.value = '' }

/** 健康提示只说明进程响应，不声称底层依赖已经验证。 */
function stateText(state: HealthState) {
  return { unchecked: '未检测', checking: '检测中', alive: '进程存活', unavailable: '不可用' }[state]
}
onMounted(checkHealth)
</script>
<template>
  <div class="app-shell">
    <div v-if="menuOpen" class="mobile-shade" @click="menuOpen = false"></div>
    <aside class="app-sidebar" :class="{ 'is-open': menuOpen }" aria-label="主导航">
      <div class="brand-wrap"><div class="brand-mark">D<span>·</span></div><div class="brand-copy"><div class="brand-name">数知融问<span class="brand-dot">.</span></div><div class="brand-subtitle">DKF DATA INTELLIGENCE</div></div></div>
      <div class="sidebar-caption">中国电子杯 · 赛题八</div>
      <nav class="nav-area"><template v-for="group in ['WORKSPACE', 'DATA & OPS']" :key="group"><div class="nav-group-title">{{ group === 'WORKSPACE' ? '智能工作台' : '数据与可解释性' }}</div>
        <button v-for="item in nav.filter(x => x.group === group)" :key="item.id" class="nav-item" :class="{ selected: current === item.id }" :aria-current="current === item.id ? 'page' : undefined" @click="navigate(item.id)"><span class="nav-icon" aria-hidden="true">{{ item.icon }}</span><span>{{ item.name }}</span></button>
      </template></nav>
      <div class="sidebar-foot"><div class="sidebar-foot-title">竞赛本地工作台</div><p>页面展示后端真实执行与引用；尚未完成端到端验收。</p><span class="sidebar-version">Vue 3 + TypeScript · v0.3</span></div>
    </aside>
    <div class="app-body"><header class="topbar"><div class="crumb"><button class="mobile-menu icon-btn" aria-label="打开导航" @click="menuOpen = !menuOpen">☰</button><span class="crumb-prefix">工作台</span><span class="crumb-slash">/</span><strong>{{ currentMeta.name }}</strong></div>
      <div class="header-right"><span class="connection-pill" :class="services.contest"><span class="status-dot"></span>问数 {{ stateText(services.contest) }}</span><span class="connection-pill" :class="services.knowledge"><span class="status-dot"></span>知识 {{ stateText(services.knowledge) }}</span><button class="icon-btn" :disabled="refreshing" aria-label="检查进程存活" @click="checkHealth">↻</button></div>
    </header>
    <div class="auth-bar"><label for="entry-token">竞赛入口令牌（仅内存）</label><input id="entry-token" v-model="tokenInput" class="input" type="password" autocomplete="off" :disabled="sql.state.busy" placeholder="填写本地配置的 CONTEST_API_TOKEN"><button class="btn small outline" :disabled="sql.state.busy" @click="applyToken">应用并清空旧会话</button><button class="btn small outline" :disabled="sql.state.busy" @click="tokenInput = ''; setToken('')">清除凭据</button><span class="hint">{{ credentials.token ? '已设置' : '未设置' }} · 同源代理不提供用户身份系统</span></div>
    <main class="app-content"><KeepAlive><component :is="activeView" :key="current" @navigate="navigate" /></KeepAlive></main></div>
  </div>
</template>
