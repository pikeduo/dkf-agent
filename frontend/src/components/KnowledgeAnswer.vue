<script setup lang="ts">
import type { RAGAnswer, SearchResponse } from '../types/knowledge.ts'
import EvidencePanel from './EvidencePanel.vue'
defineProps<{ search: SearchResponse | null; answer: RAGAnswer | null; question: string }>()
</script>
<template>
  <section v-if="search || answer" class="card card-pad stack">
    <p class="submitted-question">本次请求问题：{{ question }}</p>
    <template v-if="search"><h3>{{ search.status === 'success' ? '已检索到证据，尚未生成答案' : '检索为空，未找到可用证据' }}</h3><EvidencePanel :items="search.evidence" title="本次检索原文（非生成回答）" /></template>
    <template v-if="answer"><h3>{{ answer.status === 'success' ? '带引用的回答' : 'insufficient_evidence · 证据不足' }}</h3>
      <p class="answer-text" :class="{ 'notice warn': answer.insufficient_evidence }">{{ answer.answer }}</p>
      <EvidencePanel :items="answer.citations" title="本次回答引用" />
    </template>
  </section>
</template>
