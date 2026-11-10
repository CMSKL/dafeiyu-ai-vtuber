/* Work, speech and inspection have independent state channels on port 5500. */
(() => {
  'use strict';
  const stage = document.getElementById('stage');
  if (!stage) return;
  const params = new URLSearchParams(location.search);
  const API = 'http://127.0.0.1:5500';
  stage.dataset.aquarium = params.get('aquarium') === '0' ? 'off' : 'on';
  stage.dataset.workstation = params.get('workstation') === '0' ? 'off' : 'on';
  function node(tag, className, text, parent) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    if (parent) parent.appendChild(element);
    return element;
  }
  function svg(tag, attributes, parent) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
    if (parent) parent.appendChild(element);
    return element;
  }
  const hud = node('section', 'work-hud', undefined, stage);
  hud.setAttribute('aria-label', '工位状态');
  const heading = node('div', 'work-heading', undefined, hud);
  node('span', 'work-eyebrow', 'SHIFT / 在线工位', heading);
  const connection = node('span', 'work-connection', '连接中', heading);
  const salaryRow = node('div', 'work-salary-row', undefined, hud);
  node('span', '', '今日工资', salaryRow);
  const salary = node('strong', 'work-salary', '—', salaryRow);
  const goal = node('span', 'work-goal', '/ —', salaryRow);
  const percent = node('span', 'work-percent', '—', salaryRow);
  const track = node('div', 'work-track', undefined, hud);
  track.setAttribute('role', 'progressbar');
  track.setAttribute('aria-label', '今日工资进度');
  const progress = node('div', 'work-progress', undefined, track);
  const stats = node('div', 'work-stats', undefined, hud);
  const focus = node('span', '', '专注 —', stats);
  const slack = node('span', '', '休息 — 次', stats);
  const speed = node('span', '', '手速 ×—', stats);
  const statusRow = node('div', 'work-status-row', undefined, hud);
  const status = node('span', 'work-status', '等待状态', statusRow);
  const speech = node('span', 'work-speech', '安静待命', statusRow);
  const rule = node('p', 'work-rule', '工位守则 · 等待服务连接', hud);
  const timer = node('div', 'work-timer', undefined, hud);
  const ring = svg('svg', { viewBox: '0 0 120 120', 'aria-hidden': 'true' }, timer);
  svg('circle', { cx: 60, cy: 60, r: 52, class: 'work-ring-track' }, ring);
  const arc = svg('circle', { cx: 60, cy: 60, r: 52, class: 'work-ring' }, ring);
  const timerText = node('strong', 'work-seconds', '—', timer);
  const timerLabel = node('span', 'work-timer-label', '本轮剩余', timer);

  // All scenery is HTML/CSS and inline SVG. No bitmap assets are added.
  const scene = node('div', 'work-scene', undefined, stage);
  scene.setAttribute('aria-label', '桌子和代码显示器');
  node('div', 'work-desk-top', undefined, scene);
  node('div', 'work-desk-front', undefined, scene);
  const monitor = node('div', 'work-monitor', undefined, scene);
  const monitorBar = node('div', 'work-monitor-bar', '● ● ●     shift.workspace', monitor);
  monitorBar.setAttribute('aria-hidden', 'true');
  const screen = node('div', 'work-monitor-screen', undefined, monitor);
  const code = [
    'const shift = await beginDay();', '  // one task at a time',
    '  for (const task of today) {', '    await focus(task);',
    '    checkpoint.save();', '    breathe.in();', '  }',
    '  status.set("ready");', '', '✓ workspace is up to date',
  ];
  node('pre', 'work-code', [...code, ...code].join('\n'), screen);
  node('div', 'work-monitor-neck', undefined, monitor);
  node('div', 'work-monitor-foot', undefined, monitor);
  node('div', 'work-cup', undefined, scene);
  node('span', 'work-desk-badge', 'DESK 01 · SMALL STEPS', scene);

  const message = node('section', 'work-message', undefined, stage);
  message.setAttribute('aria-label', '当前正在回应的观众消息');
  node('span', 'work-eyebrow', '正在回应', message);
  const nickname = node('strong', 'work-message-name', '等待观众消息', message);
  const messageText = node('p', 'work-message-text', '消息会显示在这里', message);

  const eventLayer = node('section', 'work-event', undefined, stage);
  eventLayer.hidden = true;
  eventLayer.setAttribute('aria-label', '主管巡查投票');
  const supervisor = node('div', 'work-supervisor', undefined, eventLayer);
  const robot = svg('svg', { viewBox: '0 0 180 260', role: 'img', 'aria-label': '几何机器人主管' }, supervisor);
  svg('path', { d: 'M88 20V5M79 5h18', stroke: '#a9ead4', 'stroke-width': 6, 'stroke-linecap': 'round' }, robot);
  svg('rect', { x: 30, y: 25, width: 120, height: 100, rx: 28, fill: '#e9eee5', stroke: '#173b43', 'stroke-width': 6 }, robot);
  svg('rect', { x: 46, y: 47, width: 88, height: 44, rx: 14, fill: '#173b43' }, robot);
  svg('circle', { cx: 68, cy: 69, r: 7, fill: '#b4f1bf' }, robot);
  svg('circle', { cx: 112, cy: 69, r: 7, fill: '#b4f1bf' }, robot);
  svg('path', { d: 'M74 106h32', stroke: '#173b43', 'stroke-width': 5, 'stroke-linecap': 'round' }, robot);
  svg('path', { d: 'M47 138h86l27 100H20z', fill: '#37796d', stroke: '#173b43', 'stroke-width': 6, 'stroke-linejoin': 'round' }, robot);
  svg('rect', { x: 58, y: 160, width: 64, height: 50, rx: 7, fill: '#e9eee5' }, robot);
  svg('path', { d: 'm76 184 10 10 20-22', fill: 'none', stroke: '#37796d', 'stroke-width': 5 }, robot);
  const bubble = node('div', 'work-event-bubble', undefined, eventLayer);
  node('span', 'work-eyebrow', '主管巡查 / 演示', bubble);
  const question = node('p', '', '', bubble);
  const voteCard = node('div', 'work-vote-card', undefined, eventLayer);
  const voteHeading = node('div', 'work-vote-heading', undefined, voteCard);
  node('strong', '', '工位提案', voteHeading);
  const voteTimer = node('span', '', '30 秒', voteHeading);
  const options = [1, 2].map(number => {
    const row = node('div', 'work-vote-option', undefined, voteCard);
    node('b', 'work-vote-command', `发${number}`, row);
    const label = node('span', '', '', row);
    const count = node('strong', '', '0', row);
    return { label, count };
  });
  const result = node('p', 'work-vote-result', '等待投票', voteCard);
  node('small', 'work-vote-note', '演示入口投票 · 每位观众一票', voteCard);

  let work = null;
  let syncedAt = 0;
  let connected = false;
  const labels = { working: '工作中', resting: '休息中', paused: '已暂停' };
  function render(data, sentAt, receivedAt) {
    work = data.work;
    // Anchor server time to a monotonic clock and compensate half the request RTT.
    syncedAt = receivedAt - (receivedAt - sentAt) / 2;
    connection.textContent = '状态已同步';
    connected = true;
    hud.dataset.online = 'true';
    salary.textContent = Math.floor(work.salary).toLocaleString('zh-CN');
    goal.textContent = `/ ${work.salary_goal.toLocaleString('zh-CN')}`;
    const ratio = Math.min(100, work.salary / work.salary_goal * 100);
    percent.textContent = `${Math.floor(ratio)}%`;
    progress.style.width = `${ratio}%`;
    track.setAttribute('aria-valuenow', work.salary);
    track.setAttribute('aria-valuemax', work.salary_goal);
    focus.textContent = `专注 ${Math.floor(work.focus_seconds / 60)}分${Math.floor(work.focus_seconds % 60)}秒`;
    slack.textContent = `摸鱼 ${work.slack_count} 次`;
    speed.textContent = `手速 ×${work.speed_multiplier.toFixed(1)}`;
    status.textContent = labels[work.status];
    status.dataset.status = work.status;
    speech.textContent = data.speaking ? '正在说话' : data.thinking ? '正在思考' : '安静待命';
    rule.textContent = `工位守则 · ${work.rule}`;
    nickname.textContent = work.message ? work.message.nickname : '等待观众消息';
    messageText.textContent = work.message ? work.message.text : '消息会显示在这里';
    const event = work.event;
    eventLayer.hidden = !event;
    if (event) {
      question.textContent = event.question;
      event.options.forEach((label, index) => {
        options[index].label.textContent = label;
        options[index].count.textContent = `${event.counts[index]} 票`;
      });
      eventLayer.dataset.phase = event.result ? 'settled' : 'voting';
      if (event.result) {
        const winner = event.result.winner;
        result.textContent = winner ? `已截止 · ${event.options[winner - 1]} 获选` : '已截止 · 平票，维持原计划';
      } else result.textContent = '投票进行中';
    }
    if (pauseButton) pauseButton.textContent = work.status === 'paused' ? '恢复工作' : '暂停工作';
  }
  function tick(now) {
    if (work) {
      const elapsed = Math.min(3, Math.max(0, (now - syncedAt) / 1000));
      const serverNow = work.server_now + elapsed;
      const remaining = work.ends_at === null ? work.paused_remaining : Math.max(0, work.ends_at - serverNow);
      timerText.textContent = `${Math.ceil(remaining)}`;
      timerLabel.textContent = work.status === 'paused' ? '暂停保留' : '本轮剩余 / 秒';
      const circumference = 2 * Math.PI * 52;
      arc.style.strokeDasharray = `${circumference}`;
      arc.style.strokeDashoffset = `${circumference * (1 - Math.max(0, Math.min(1, remaining / work.round_seconds)))}`;
      if (work.event) voteTimer.textContent = work.event.result ? '已结算' : `${Math.ceil(Math.max(0, work.event.ends_at - serverNow))} 秒`;
      if (connected && now - syncedAt > 3000) {
        connected = false;
        connection.textContent = '连接中断 · 保留最后状态';
        hud.dataset.online = 'false';
      }
    }
    requestAnimationFrame(tick);
  }
  async function poll() {
    const sentAt = performance.now();
    try {
      const response = await fetch(`${API}/state`, { cache: 'no-store', signal: AbortSignal.timeout(1500) });
      if (!response.ok) throw new Error('state unavailable');
      const data = await response.json();
      if (!data.work) throw new Error('work state unavailable');
      render(data, sentAt, performance.now());
    } catch {
      connected = false;
      connection.textContent = '服务离线 · 等待重连';
      hud.dataset.online = 'false';
    }
    setTimeout(poll, 500);
  }

  // Explicitly opt in to local demo controls. Audience text is never parsed as a command.
  let pauseButton = null;
  if (params.get('controls') === '1') {
    const controls = node('aside', 'work-controls', undefined, stage);
    const feedback = node('span', 'work-control-feedback', '本地演示控制', controls);
    async function command(path, payload) {
      try {
        const response = await fetch(`${API}${path}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload), signal: AbortSignal.timeout(2000),
        });
        const answer = await response.json();
        if (!response.ok) throw new Error(answer.error);
        feedback.textContent = answer.reason || '已发送';
      } catch (error) { feedback.textContent = `操作失败 · ${error.message}`; }
    }
    pauseButton = node('button', '', '暂停工作', controls);
    pauseButton.onclick = () => command('/work/control', { action: work && work.status === 'paused' ? 'resume' : 'pause' });
    node('button', '', '主管巡查', controls).onclick = () => command('/work/event', {});
    const user = node('input', '', undefined, controls);
    user.placeholder = '投票用户 ID';
    user.value = 'local-viewer';
    user.setAttribute('aria-label', '投票用户 ID');
    [1, 2].forEach(option => {
      node('button', '', `发${option}`, controls).onclick = () => command('/work/vote', { event_id: work && work.event && work.event.id, user_id: user.value, option });
    });
    node('button', '', '消息示例', controls).onclick = () => command('/work/message', { nickname: '路过的同事', text: '今天准备先完成哪件小事？' });
  }
  poll();
  requestAnimationFrame(tick);
})();
