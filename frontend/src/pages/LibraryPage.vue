<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { knowledgeApi } from '../state/workbench.ts'
import { createLibraryWorkspace } from '../state/library.ts'
import { statusTitle, pageLabel } from '../utils/format.ts'
import Pager from '../components/Pager.vue'
import ParseQuality from '../components/ParseQuality.vue'
const model = createLibraryWorkspace(knowledgeApi)
const s = model.state
const files = ref<File[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const uploadCloud = ref(false)
const retryCloud = ref(false)
// 进入页面只读知识库目录；上传或重试必须另行点击。
onMounted(() => { void model.bases() })

/** 保留原 File 对象和顺序，不因选文件而读取正文或自动上传。 */
function chooseFiles(event: Event) {
  files.value = Array.from((event.target as HTMLInputElement).files || [])
  uploadCloud.value = false
}

/** 上传每次单独确认auto路由可能产生的MinerU费用，接收后清空本次选择。 */
async function upload() {
  const snapshot = [...files.value]
  await model.upload(snapshot, uploadCloud.value)
  uploadCloud.value = false
  if (s.upload) { files.value = []; if (fileInput.value) fileInput.value.value = '' }
}

/** 默认native恢复，不授权PDF新增分片；云恢复需当次另行确认。 */
async function retry() {
  await model.retry(retryCloud.value)
  retryCloud.value = false
}

/** 库切换清空已选文件许可与详情，避免误把旧选择上传到新范围。 */
async function selectBase(event: Event) {
  files.value = []; uploadCloud.value = false; retryCloud.value = false
  if (fileInput.value) fileInput.value.value = ''
  await model.selectBase((event.target as HTMLSelectElement).value)
}
</script>
<template>
  <div class="page stack">
    <header class="page-header"><div><div class="eyebrow">LIBRARY / INGESTION</div><h1 class="page-title">知识库与文档管理</h1><p class="page-subtitle">上传接收、处理任务和向量入库具有不同状态，点击刷新读取真实进度。</p></div></header>
    <section class="card card-pad stack"><h3>创建知识库</h3><fieldset :disabled="s.busy"><div class="form-grid"><label>名称<input v-model="s.kbName" class="input" maxlength="255"></label><label>说明<input v-model="s.description" class="input" maxlength="2000"></label></div><button class="btn primary" :disabled="!s.kbName.trim()" @click="model.createBase">创建知识库</button></fieldset></section>
    <section class="card card-pad stack">
      <div class="panel-heading"><h3>知识库目录</h3><button class="btn outline" :disabled="s.busy" @click="model.bases(s.bases.offset)">刷新目录</button></div>
      <select class="select" :value="s.kbId" :disabled="s.busy" @change="selectBase"><option value="">请选择知识库</option><option v-for="base in s.bases.items" :key="base.kb_id" :value="base.kb_id" :disabled="base.status !== 'ACTIVE'">{{ base.name }} · {{ base.status }}</option></select>
      <p v-if="s.kbId" class="identifier">当前范围：{{ s.kbId }}</p><Pager v-bind="s.bases" :busy="s.busy" @change="model.bases" />
      <template v-if="s.kbId">
        <h3>上传文档（1～50 份）</h3><input ref="fileInput" type="file" multiple :disabled="s.busy" @change="chooseFiles">
        <p v-if="files.length" class="hint">已选 {{ files.length }} 份：{{ files.map(file => file.name).join('、') }}</p>
        <label class="check"><input v-model="uploadCloud" type="checkbox" :disabled="s.busy">本次允许 auto 路由按需外传原件到 MinerU，并接受费用</label>
        <p class="hint">不勾选时显式 native，不会用云端弥补格式或视觉缺口。auto 的文档许可不通用于 SQL/RAG。</p>
        <button class="btn primary" :disabled="s.busy || !files.length || files.length > 50" @click="upload">上传所选文件</button>
      </template>
    </section>
    <p v-if="s.error" role="alert" class="notice danger">{{ s.error }}</p><p v-if="s.info" class="notice">{{ s.info }}</p>
    <section v-if="s.upload" class="card card-pad"><h3>本次上传接收报告</h3>
      <template v-if="'items' in s.upload"><p>接收 {{ s.upload.accepted_count }} / 失败 {{ s.upload.failed_count }}，总计 {{ s.upload.total }}。</p>
        <div v-for="item in s.upload.items" :key="item.index" class="evidence-item"><strong>{{ item.file_name || '未返回文件名' }} · HTTP {{ item.status_code }}</strong><p class="identifier">doc_id：{{ item.doc_id || item.document?.doc_id || '未返回，请刷新目录核对' }}</p><p v-if="item.document">{{ item.document.status }} · {{ statusTitle(item.document.status) }}</p><p v-else>本项未返回文档对象；无处理完成凭证。</p><p v-if="item.status_code >= 400" class="notice warn">该文件未完整接收，先核对已有 doc_id，勿自动重传。</p></div>
      </template>
      <template v-else><p>{{ s.upload.file_name }} · {{ s.upload.status }} / {{ statusTitle(s.upload.status) }}</p><p class="identifier">{{ s.upload.doc_id }}</p></template>
      <p class="hint">这是服务端接收报告；后台最终状态请通过下方文档状态查询。</p>
    </section>
    <section v-if="s.kbId" class="card card-pad stack"><div class="panel-heading"><h3>文档列表</h3><button class="btn outline" :disabled="s.busy" @click="model.documents(s.docs.offset)">刷新文档</button></div>
      <div class="table-scroll"><table class="data-table"><thead><tr><th>文件</th><th>真实状态</th><th>操作</th></tr></thead><tbody><tr v-for="doc in s.docs.items" :key="doc.doc_id"><td>{{ doc.file_name }}<small class="identifier">{{ doc.doc_id }}</small></td><td>{{ doc.status }}<small>{{ statusTitle(doc.status) }}</small></td><td><button class="btn small outline" :disabled="s.busy" @click="retryCloud = false; model.inspect(doc)">查询实时状态</button></td></tr></tbody></table></div>
      <Pager v-bind="s.docs" :busy="s.busy" @change="model.documents" />
    </section>
    <section v-if="s.selected" class="card card-pad stack"><h3>{{ s.selected.file_name }}</h3>
      <p v-if="!s.status" class="notice warn">尚未取得本次状态快照，不能推断处理成功。</p>
      <template v-else><p class="pill">{{ s.status.status }} · {{ statusTitle(s.status.status) }}</p><p class="identifier">状态更新时间 {{ s.status.updated_at }}</p>
        <p v-if="s.status.status === 'PARTIAL_READY'" class="notice warn">部分内容已入库；存在解析/页覆盖缺口，不能宣称完整可用。</p>
        <p v-if="s.status.status === 'FAILED'" class="notice danger">文档处理失败；请检查安全日志及管理状态，不显示原始异常。</p>
        <dl class="metrics"><div><dt>Block</dt><dd>{{ s.status.block_count }}</dd></div><div><dt>Chunk</dt><dd>{{ s.status.chunk_count }}</dd></div><div><dt>向量</dt><dd>{{ s.status.vector_count }}</dd></div><div><dt>无效向量</dt><dd>{{ s.status.invalid_vector_count }}</dd></div></dl>
        <ParseQuality :quality="s.status.parse_quality" />
        <p v-if="s.status.management" class="identifier">编排 {{ s.status.management.management_task_id }} · {{ s.status.management.stage }} / {{ s.status.management.state }}</p>
        <label class="check"><input v-model="retryCloud" type="checkbox" :disabled="s.busy">本次恢复允许 auto 云路由及可能费用（不授权新增PDF片）</label>
        <div class="toolbar"><button class="btn outline" :disabled="s.busy" @click="model.inspect(s.selected!)">刷新状态</button><button class="btn outline" :disabled="s.busy || !['FAILED', 'UPLOADED', 'CHUNKING', 'EMBEDDING', 'INDEXING'].includes(s.status.status)" @click="retry">手动恢复失败/待续阶段</button><button class="btn outline" :disabled="s.busy" @click="model.evidence('blocks')">读取Block</button><button class="btn outline" :disabled="s.busy" @click="model.evidence('chunks')">读取Chunk</button></div>
        <p class="hint">活动任务会被后端拒绝，页面不盲重投。未接入删除、Force Reparse和新增分片操作。</p>
      </template>
      <template v-if="s.evidenceMode === 'blocks'"><h4>Block 原文</h4><article v-for="block in s.blocks.items" :key="block.block_id" class="evidence-item"><strong>{{ block.block_type }} · 阅读序号 {{ block.block_index ?? '未记录' }} · 原页码 {{ block.page }}</strong><p>{{ block.section ?? '章节未标注' }}</p><pre>{{ block.text }}</pre><p class="identifier">{{ block.block_id }}</p></article><Pager v-bind="s.blocks" :busy="s.busy" @change="model.evidence('blocks', $event)" /></template>
      <template v-if="s.evidenceMode === 'chunks'"><h4>Chunk 原文</h4><article v-for="chunk in s.chunks.items" :key="chunk.chunk_id" class="evidence-item"><strong>{{ pageLabel({ ...chunk, file_type: s.selected.file_type }) }}</strong><pre>{{ chunk.text }}</pre><p class="identifier">{{ chunk.chunk_id }}</p></article><Pager v-bind="s.chunks" :busy="s.busy" @change="model.evidence('chunks', $event)" /></template>
    </section>
  </div>
</template>
