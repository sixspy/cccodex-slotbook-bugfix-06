export const waitlistViewTemplate = `
  <div v-show="tab===2" class="panel">
    <h2>候补队列</h2>
    <p class="hint">名次册落库：取消即同事务重排，后面的人自动递补；可晋升头每档唯一。</p>
    <p class="err" v-if="actionErr">{{ actionErr }}</p>
    <table>
      <thead><tr><th>名次</th><th>ID</th><th>档</th><th>当事人</th><th>席</th><th>原因</th><th>态</th><th>可晋升头</th><th></th></tr></thead>
      <tbody>
        <tr v-for="w in waitlist" :key="w.id" :class="{head: w.promotable}">
          <td class="rank">{{ w.status==='候补中' ? w.queue_rank : '—' }}</td>
          <td>{{ w.id }}</td><td>{{ w.slot_code }}</td><td>{{ w.party_key }}</td>
          <td>{{ w.seats }}</td><td>{{ w.reason }}</td><td>{{ w.status }}</td>
          <td><span v-if="w.promotable" class="tag">可晋升头</span></td>
          <td>
            <button class="ghost" v-if="w.status==='候补中'" @click="promoteWait(w.id)">晋升占位</button>
            <button class="ghost danger" v-if="w.status==='候补中'" @click="cancelWait(w.id)">取消候补</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
`;
