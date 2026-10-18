<script setup lang="ts">
import type { createKnowledgeWorkspace } from '../state/knowledge.ts'
import Pager from './Pager.vue'
import { statusTitle } from '../utils/format.ts'
const props = defineProps<{ model: ReturnType<typeof createKnowledgeWorkspace> }>()
const s = props.model.state
/** 从真实库选项选择范围，变更后清除旧文档和本次许可。 */
function chooseBase(event: Event) { void props.model.selectBase((event.target as HTMLSelectElement).value) }
</script>
<template>
  <div class="stack">
    <div class="panel-heading"><label>知识库范围</label><button class="btn small outline" :disabled="s.busy" @click="model.bases()">刷新知识库</button></div>
    <select class="select" :value="s.kbId" :disabled="s.busy" @change="chooseBase">
      <option value="">请选择真实知识库</option>
      <option v-for="base in s.bases.items" :key="base.kb_id" :value="base.kb_id" :disabled="base.status !== 'ACTIVE'">{{ base.name }} · {{ base.status }}</option>
    </select>
    <p v-if="s.kbId && !s.bases.items.some(b => b.kb_id === s.kbId)" class="identifier">当前选定知识库：{{ s.kbId }}（位于其他目录页）</p>
    <Pager v-bind="s.bases" :busy="s.busy" @change="model.bases" />
    <template v-if="s.kbId">
      <div class="panel-heading"><label class="check"><input v-model="s.allDocuments" type="checkbox" :disabled="s.busy" @change="model.invalidate">全库合格文档</label><button class="btn small outline" :disabled="s.busy" @click="model.documents(s.docs.offset)">刷新文档</button></div>
      <p class="hint">取消全库后必须选择 1～100 份文档；跨页选择保留，PARTIAL_READY 内容有缺口。</p>
      <div class="document-options">
        <label v-for="doc in s.docs.items" :key="doc.doc_id" class="check">
          <input type="checkbox" :checked="s.docIds.includes(doc.doc_id)" :disabled="s.busy || s.allDocuments || !['READY', 'PARTIAL_READY'].includes(doc.status)"
            @change="model.toggleDocument(doc, ($event.target as HTMLInputElement).checked)">
          {{ doc.file_name }} · {{ doc.status }} / {{ statusTitle(doc.status) }}
        </label>
      </div>
      <p v-if="s.docsLoaded && !s.docs.total" class="empty">该库暂未返回文档。</p>
      <Pager v-bind="s.docs" :busy="s.busy" @change="model.documents" />
      <details v-if="!s.allDocuments && s.docIds.length"><summary>已选 {{ s.docIds.length }} 份（含其他页）</summary><p v-for="id in s.docIds" :key="id" class="identifier">{{ id }}</p><button class="btn small outline" :disabled="s.busy" @click="s.docIds = []; model.invalidate()">清空文档选择</button></details>
    </template>
  </div>
</template>
