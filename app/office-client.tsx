'use client';

import {
  FormEvent,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AgentModel,
  AgentReport,
  COMPUTER_STATIONS,
  Direction,
  HEARING_RADIUS,
  MEETING_SEATS,
  WorkEvent,
  canSee,
  directionFromDelta,
  distance,
  hearingRecipients,
  kstReviewCycle,
  movePoint,
  transitionVisibility,
} from '@/lib/world';
import {
  BOARD_PALETTE,
  BoardTextBlock,
  WhiteboardDocument,
  base64ToBytes,
  bytesToBase64,
  createBlankBoardPixels,
  linePoints,
} from '@/lib/whiteboard';

type Viewer = { displayName: string; email: string } | null;
type DetailTab = 'plan' | 'activity' | 'messages' | 'report' | 'memory';
type ModalName = 'whiteboard' | 'computer' | 'meeting-room' | 'review' | 'notifications' | 'reports' | null;
type ChatMessage = { id: string; speaker: string; body: string; at: string; kind: 'user' | 'agent' | 'system'; heardBy?: string[]; planId?: string };
type JobAudit = { job: { id: string; command: string; status: string; phase: string; events: WorkEvent[] } };
type WorldSnapshot = {
  worldVersion: number;
  serverTime: string;
  agentsPaused: boolean;
  agents: AgentModel[];
  messages: ChatMessage[];
  reports: AgentReport[];
  player?: { position: { x: number; y: number }; facing: Direction; updatedAt: string };
  whiteboard: WhiteboardDocument;
  reviewSubmitted?: boolean;
};

const TOOL_LABELS = { whiteboard: '화이트보드', computer: '컴퓨터', 'meeting-room': '회의실' } as const;
const EMPTY_BOARD: WhiteboardDocument = {
  title: '공용 보드', width: 256, height: 256, palette: [...BOARD_PALETTE],
  pixelsBase64: bytesToBase64(createBlankBoardPixels()), textBlocks: [], version: 1,
  authorType: 'system', authorId: 'system', authorName: '시스템', updatedAt: new Date(0).toISOString(), history: [],
};

function formatTime(value: string | Date) {
  return new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul' }).format(new Date(value));
}

function safeInitialDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed;
}

function preferredMeetingSeat(agentId: string) {
  return MEETING_SEATS.find((seat) => seat.id === `seat-${agentId}`) ?? MEETING_SEATS[0];
}

function eventLabel(type: WorkEvent['type']) {
  return ({ command: '명령', decision: '판단 요약', tool_request: '도구 전달', tool_result: '도구 반환', agent_message: '에이전트 메시지', verification: '검증', status: '상태', report: '리포트' })[type];
}

function PixelAgent({ agent, selected, observed, paused, onSelect }: { agent: AgentModel; selected: boolean; observed: boolean; paused: boolean; onSelect: (event: ReactMouseEvent) => void }) {
  return (
    <button
      className={`pixel-agent ${selected ? 'selected' : ''} ${observed ? 'in-view' : 'out-of-view'}`}
      style={{ left: `${agent.position.x}%`, top: `${agent.position.y}%` }}
      onClick={onSelect}
      onContextMenu={onSelect}
      aria-label={`${agent.name}, ${paused ? `일시정지 · ${agent.activity}` : agent.activity}. ${observed ? '시야 안' : '선택 에이전트의 시야 밖'}`}
      type="button"
    >
      <span className="agent-label"><strong>{agent.name}</strong> · {paused ? '일시정지' : agent.activity}</span>
      <span className={`agent-sprite ${agent.tone} face-${agent.facing}`} aria-hidden="true"><i className="agent-hair" /><i className="agent-face" /><i className="agent-body" /></span>
      {agent.currentTool && <span className="working-pulse" />}
    </button>
  );
}

function drawBoard(canvas: HTMLCanvasElement | null, pixels: Uint8Array, palette: string[]) {
  if (!canvas) return;
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) return;
  const image = context.createImageData(256, 256);
  palette.forEach((color, index) => {
    const value = color.replace('#', '');
    (drawBoard as unknown as { colors?: Array<[number, number, number]> }).colors ??= [];
    (drawBoard as unknown as { colors: Array<[number, number, number]> }).colors[index] = [parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16)];
  });
  const colors = (drawBoard as unknown as { colors: Array<[number, number, number]> }).colors;
  for (let index = 0; index < pixels.length; index += 1) {
    const [red, green, blue] = colors[pixels[index]] ?? colors[0];
    const offset = index * 4;
    image.data[offset] = red;
    image.data[offset + 1] = green;
    image.data[offset + 2] = blue;
    image.data[offset + 3] = 255;
  }
  context.putImageData(image, 0, 0);
}

function DecisionCard({ event }: { event: WorkEvent }) {
  const summary = event.summary;
  if (!summary) return null;
  return (
    <article className="decision-card">
      <header><b>{event.title}</b><time>{formatTime(event.at)}</time></header>
      <dl>
        <div><dt>관찰</dt><dd>{summary.observations.join(' · ')}</dd></div>
        <div><dt>목표</dt><dd>{summary.objective}</dd></div>
        <div><dt>선택 행동</dt><dd>{summary.chosenAction}</dd></div>
        <div><dt>선택 이유</dt><dd>{summary.rationale}</dd></div>
        {!!summary.alternatives.length && <div><dt>검토한 대안</dt><dd>{summary.alternatives.map((item) => `${item.action} — ${item.rejectedBecause}`).join(' / ')}</dd></div>}
        {!!summary.evidenceRefs.length && <div><dt>근거</dt><dd>{summary.evidenceRefs.join(' · ')}</dd></div>}
      </dl>
      <small>공개 판단 신뢰도 {Math.round(summary.confidence * 100)}% · 숨은 사고과정이 아닌 검증 가능한 요약</small>
    </article>
  );
}

export default function OfficeClient({ viewer, canControlAgents, initialNow }: { viewer: Viewer; canControlAgents: boolean; initialNow: string }) {
  const [agents, setAgents] = useState<AgentModel[]>([]);
  const [selectedId, setSelectedId] = useState('minji');
  const [detailTab, setDetailTab] = useState<DetailTab>('plan');
  const [player, setPlayer] = useState({ x: 49, y: 54 });
  const [playerFacing, setPlayerFacing] = useState<'north' | 'east' | 'south' | 'west'>('south');
  const [visionLayer, setVisionLayer] = useState(true);
  const [agentsPaused, setAgentsPaused] = useState(true);
  const [controlReady, setControlReady] = useState(false);
  const [controlSaving, setControlSaving] = useState(false);
  const [runtimeOnline, setRuntimeOnline] = useState(false);
  const [worldVersion, setWorldVersion] = useState(0);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [reports, setReports] = useState<AgentReport[]>([]);
  const [jobAudit, setJobAudit] = useState<JobAudit | null>(null);
  const [jobAuditLoading, setJobAuditLoading] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [commandSending, setCommandSending] = useState(false);
  const [modal, setModal] = useState<ModalName>(null);
  const [toast, setToast] = useState('');
  const [clock, setClock] = useState(() => safeInitialDate(initialNow));
  const [whiteboard, setWhiteboard] = useState<WhiteboardDocument>(EMPTY_BOARD);
  const [boardPreview, setBoardPreview] = useState<WhiteboardDocument | null>(null);
  const [boardVersionLoading, setBoardVersionLoading] = useState(false);
  const [boardDirty, setBoardDirty] = useState(false);
  const [brushIndex, setBrushIndex] = useState(1);
  const [brushSize, setBrushSize] = useState(3);
  const [reviewScores, setReviewScores] = useState({ individual: 4, team: 4, comment: '' });
  const [reviewSubmitted, setReviewSubmitted] = useState(false);
  const [observationLog, setObservationLog] = useState<Array<{ id: string; body: string; at: string }>>([]);
  const keys = useRef(new Set<string>());
  const previousVisible = useRef(new Set<string>());
  const agentsPausedRef = useRef(true);
  const runtimeOnlineRef = useRef(false);
  const playerRef = useRef(player);
  const playerFacingRef = useRef<Direction>(playerFacing);
  const playerDirtyRef = useRef(false);
  const playerSyncBusy = useRef(false);
  const controlChannel = useRef<BroadcastChannel | null>(null);
  const syncBusy = useRef(false);
  const boardDirtyRef = useRef(false);
  const boardPreviewRef = useRef(false);
  const modalRef = useRef<ModalName>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const pixelsRef = useRef(createBlankBoardPixels());
  const previewPixelsRef = useRef(createBlankBoardPixels());
  const lastSyncedBoardRef = useRef<WhiteboardDocument>(EMPTY_BOARD);
  const lastSyncedPixelsRef = useRef(createBlankBoardPixels());
  const paintRef = useRef<{ active: boolean; last: { x: number; y: number } | null }>({ active: false, last: null });

  const selected = agents.find((agent) => agent.id === selectedId) ?? agents[0] ?? null;
  const reviewCycle = useMemo(() => kstReviewCycle(clock), [clock]);
  const visibleIds = useMemo(() => {
    if (!selected) return new Set<string>();
    if (!visionLayer) return new Set(agents.map((agent) => agent.id));
    return new Set(agents.filter((agent) => agent.id === selected.id || canSee(selected, agent.position)).map((agent) => agent.id));
  }, [agents, selected, visionLayer]);
  const nearby = useMemo(() => hearingRecipients(player, agents), [player, agents]);
  const computerOwners = COMPUTER_STATIONS.map((station) => ({ station, owner: agents.find((agent) => agent.currentTool === 'computer' && agent.toolStation === station.id) }));
  const displayedBoard = boardPreview ?? whiteboard;
  const boardReadOnly = boardPreview !== null;

  useEffect(() => { boardDirtyRef.current = boardDirty; }, [boardDirty]);
  useEffect(() => { boardPreviewRef.current = boardPreview !== null; }, [boardPreview]);
  useEffect(() => { modalRef.current = modal; }, [modal]);

  const refreshWorld = useCallback(async (quiet = false) => {
    if (syncBusy.current) return;
    syncBusy.current = true;
    try {
      const response = await fetch('/api/bootstrap', { cache: 'no-store' });
      const data = await response.json() as WorldSnapshot & { error?: string };
      if (!response.ok || !Array.isArray(data.agents)) throw new Error(data.error ?? '공유 월드를 불러오지 못했습니다.');
      setAgents(data.agents);
      setMessages(Array.isArray(data.messages) ? data.messages : []);
      setReports(Array.isArray(data.reports) ? data.reports : []);
      agentsPausedRef.current = Boolean(data.agentsPaused);
      setAgentsPaused(Boolean(data.agentsPaused));
      setReviewSubmitted(Boolean(data.reviewSubmitted));
      setWorldVersion(Number(data.worldVersion ?? 0));
      setRuntimeOnline(true);
      runtimeOnlineRef.current = true;
      setControlReady(true);
      if (data.player && !playerDirtyRef.current && keys.current.size === 0) {
        playerRef.current = data.player.position;
        playerFacingRef.current = data.player.facing;
        setPlayer(data.player.position);
        setPlayerFacing(data.player.facing);
      }
      if (data.whiteboard) {
        let serverPixels: Uint8Array;
        try { serverPixels = base64ToBytes(data.whiteboard.pixelsBase64); } catch { serverPixels = createBlankBoardPixels(); }
        lastSyncedBoardRef.current = data.whiteboard;
        lastSyncedPixelsRef.current = serverPixels.slice();
        if (modalRef.current !== 'whiteboard' || !boardDirtyRef.current) {
          setWhiteboard(data.whiteboard);
          pixelsRef.current = serverPixels;
          if (!boardPreviewRef.current) requestAnimationFrame(() => drawBoard(canvasRef.current, pixelsRef.current, data.whiteboard.palette));
        }
      }
    } catch (error) {
      setRuntimeOnline(false);
      runtimeOnlineRef.current = false;
      if (!controlReady) {
        agentsPausedRef.current = true;
        setAgentsPaused(true);
        setControlReady(true);
      }
      if (!quiet) setToast(error instanceof Error ? error.message : '공유 월드 연결에 실패했습니다.');
    } finally {
      syncBusy.current = false;
    }
  }, [controlReady]);

  useEffect(() => {
    const initialPoll = window.setTimeout(() => void refreshWorld(), 0);
    const poll = () => { if (document.visibilityState === 'visible') void refreshWorld(true); };
    const timer = window.setInterval(poll, 800);
    window.addEventListener('focus', poll);
    document.addEventListener('visibilitychange', poll);
    if ('BroadcastChannel' in window) {
      const channel = new BroadcastChannel('cashcow-world-state');
      controlChannel.current = channel;
      channel.addEventListener('message', poll);
    }
    return () => {
      window.clearTimeout(initialPoll);
      window.clearInterval(timer);
      window.removeEventListener('focus', poll);
      document.removeEventListener('visibilitychange', poll);
      controlChannel.current?.close();
      controlChannel.current = null;
    };
  }, [refreshWorld]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(''), 3400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    const down = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || (event.target as HTMLElement)?.isContentEditable) return;
      const key = event.key.toLowerCase();
      if (['arrowup', 'arrowdown', 'arrowleft', 'arrowright', 'w', 'a', 's', 'd'].includes(key)) {
        event.preventDefault();
        keys.current.add(key);
      }
    };
    const up = (event: KeyboardEvent) => keys.current.delete(event.key.toLowerCase());
    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    const mover = window.setInterval(() => {
      const pressed = keys.current;
      const dx = (pressed.has('d') || pressed.has('arrowright') ? .6 : 0) - (pressed.has('a') || pressed.has('arrowleft') ? .6 : 0);
      const dy = (pressed.has('s') || pressed.has('arrowdown') ? .6 : 0) - (pressed.has('w') || pressed.has('arrowup') ? .6 : 0);
      if (dx || dy) {
        setPlayer((current) => {
          const next = movePoint(current, dx, dy);
          playerRef.current = next;
          playerDirtyRef.current = true;
          return next;
        });
        setPlayerFacing((current) => {
          const next = directionFromDelta(dx, dy, current);
          playerFacingRef.current = next;
          return next;
        });
      }
    }, 42);
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); window.clearInterval(mover); };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(async () => {
      if (!playerDirtyRef.current || playerSyncBusy.current || !runtimeOnlineRef.current) return;
      playerSyncBusy.current = true;
      playerDirtyRef.current = false;
      const desired = playerRef.current;
      try {
        const response = await fetch('/api/player', {
          method: 'PATCH', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ ...desired, facing: playerFacingRef.current }),
        });
        const authoritative = await response.json() as { position?: { x: number; y: number }; facing?: Direction };
        if (!response.ok || !authoritative.position || !authoritative.facing) throw new Error('대표 위치 동기화 실패');
        if (keys.current.size === 0) {
          playerRef.current = authoritative.position;
          playerFacingRef.current = authoritative.facing;
          setPlayer(authoritative.position);
          setPlayerFacing(authoritative.facing);
        }
      } catch {
        playerDirtyRef.current = true;
      } finally {
        playerSyncBusy.current = false;
      }
    }, 120);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const changes = transitionVisibility(previousVisible.current, visibleIds);
    if (changes.length) {
      setObservationLog((current) => [
        ...changes.map((change) => ({
          id: crypto.randomUUID(),
          body: change.type === 'seen'
            ? `${agents.find((agent) => agent.id === change.id)?.name ?? '캐릭터'} 발견 · 서버 활동 상태 확인`
            : `${agents.find((agent) => agent.id === change.id)?.name ?? '캐릭터'} 시야에서 사라짐`,
          at: new Date().toISOString(),
        })),
        ...current,
      ].slice(0, 8));
    }
    previousVisible.current = new Set(visibleIds);
  }, [visibleIds, agents]);

  useEffect(() => {
    if (modal !== 'whiteboard') return;
    requestAnimationFrame(() => drawBoard(canvasRef.current, boardPreview ? previewPixelsRef.current : pixelsRef.current, displayedBoard.palette));
  }, [modal, boardPreview, displayedBoard.palette]);

  function selectAgent(event: ReactMouseEvent, id: string) {
    event.preventDefault();
    setSelectedId(id);
    setDetailTab('plan');
  }

  async function sendCommand(messageOverride?: string) {
    if (commandSending) return;
    if (!controlReady || agentsPausedRef.current || !runtimeOnline) {
      setToast(!runtimeOnline ? '영속 월드 서버에 다시 연결한 뒤 명령해 주세요.' : '에이전트가 일시정지 상태입니다.');
      return;
    }
    const body = (messageOverride ?? draft).trim();
    if (!body) return;
    const mention = agents.find((agent) => body.includes(`@${agent.name}`));
    const heard = hearingRecipients(player, agents, HEARING_RADIUS);
    const primary = mention ?? heard[0];
    if (!primary) { setToast('현재 대화를 들을 수 있는 에이전트가 없습니다.'); return; }
    setDraft('');
    setCommandSending(true);
    try {
      const response = await fetch('/api/agent', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ agentId: primary.id, command: body, idempotencyKey: crypto.randomUUID() }),
      });
      const data = await response.json() as { reply?: string; jobId?: string; error?: string };
      if (!response.ok) throw new Error(data.error ?? '명령을 접수하지 못했습니다.');
      setSelectedId(primary.id);
      setDetailTab('plan');
      setToast(data.reply ?? `${primary.name}의 영속 작업이 시작됐습니다.`);
      controlChannel.current?.postMessage({ type: 'world-changed' });
      await refreshWorld(true);
    } catch (error) {
      setToast(error instanceof Error ? error.message : '명령 전달에 실패했습니다.');
    } finally {
      setCommandSending(false);
    }
  }

  function submitChat(event: FormEvent) { event.preventDefault(); void sendCommand(); }

  async function toggleAgents() {
    if (!canControlAgents || !controlReady || controlSaving || !runtimeOnline) return;
    const nextPaused = !agentsPausedRef.current;
    setControlSaving(true);
    try {
      const response = await fetch('/api/world-control', { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ agentsPaused: nextPaused }) });
      const data = await response.json() as { agentsPaused?: boolean; error?: string };
      if (!response.ok || typeof data.agentsPaused !== 'boolean') throw new Error(data.error ?? '활동 상태를 저장하지 못했습니다.');
      agentsPausedRef.current = data.agentsPaused;
      setAgentsPaused(data.agentsPaused);
      controlChannel.current?.postMessage({ type: 'world-changed' });
      setToast(data.agentsPaused ? '상시 실행 서버의 이동·도구 호출을 정지했습니다.' : '상시 실행 서버가 체크포인트부터 작업을 재개했습니다.');
    } catch (error) {
      setToast(error instanceof Error ? error.message : '활동 상태 저장에 실패했습니다.');
    } finally {
      setControlSaving(false);
    }
  }

  async function submitReview() {
    if (!runtimeOnline || agentsPausedRef.current) { setToast('에이전트 활동을 재개한 뒤 평가해 주세요.'); return; }
    try {
      const response = await fetch('/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ cycleId: reviewCycle.id, ...reviewScores, agentIds: agents.map((agent) => agent.id) }) });
      const data = await response.json() as { error?: string };
      if (!response.ok) throw new Error(data.error ?? '평가를 저장하지 못했습니다.');
      setReviewSubmitted(true);
      setModal(null);
      setToast('주간 평가가 서버 기억과 다음 작업 정책에 반영됐습니다.');
      await refreshWorld(true);
    } catch (error) { setToast(error instanceof Error ? error.message : '평가 저장에 실패했습니다.'); }
  }

  function canvasPoint(event: ReactPointerEvent<HTMLCanvasElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(255, Math.floor(((event.clientX - rect.left) / rect.width) * 256))),
      y: Math.max(0, Math.min(255, Math.floor(((event.clientY - rect.top) / rect.height) * 256))),
    };
  }

  function paintLine(from: { x: number; y: number }, to: { x: number; y: number }) {
    const pixels = pixelsRef.current;
    const radius = Math.max(0, Math.floor((brushSize - 1) / 2));
    linePoints(from, to).forEach((point) => {
      for (let y = -radius; y <= radius; y += 1) for (let x = -radius; x <= radius; x += 1) {
        const px = point.x + x;
        const py = point.y + y;
        if (px >= 0 && px < 256 && py >= 0 && py < 256 && x * x + y * y <= radius * radius + 1) pixels[py * 256 + px] = brushIndex;
      }
    });
    setBoardDirty(true);
    drawBoard(canvasRef.current, pixels, whiteboard.palette);
  }

  function beginPaint(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (boardReadOnly) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const point = canvasPoint(event);
    paintRef.current = { active: true, last: point };
    paintLine(point, point);
  }

  function continuePaint(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!paintRef.current.active || !paintRef.current.last) return;
    const point = canvasPoint(event);
    paintLine(paintRef.current.last, point);
    paintRef.current.last = point;
  }

  function endPaint() { paintRef.current = { active: false, last: null }; }

  function addTextBlock() {
    if (boardReadOnly) return;
    const next: BoardTextBlock = {
      id: crypto.randomUUID(), x: 36, y: 36, width: 132, height: 58, text: '새 텍스트 영역',
      color: '#26394f', background: '#f9f4dfef', fontSize: 10, authorName: viewer?.displayName ?? '대표님',
    };
    setWhiteboard((current) => ({ ...current, textBlocks: [...current.textBlocks, next] }));
    setBoardDirty(true);
  }

  function updateTextBlock(id: string, patch: Partial<BoardTextBlock>) {
    if (boardReadOnly) return;
    setWhiteboard((current) => ({ ...current, textBlocks: current.textBlocks.map((block) => block.id === id ? { ...block, ...patch } : block) }));
    setBoardDirty(true);
  }

  function startBlockGesture(event: ReactPointerEvent<HTMLButtonElement>, block: BoardTextBlock, mode: 'move' | 'resize') {
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startY = event.clientY;
    const origin = { ...block };
    const stage = event.currentTarget.closest('.board-stage')?.getBoundingClientRect();
    if (!stage) return;
    const onMove = (move: PointerEvent) => {
      const dx = ((move.clientX - startX) / stage.width) * 256;
      const dy = ((move.clientY - startY) / stage.height) * 256;
      if (mode === 'move') updateTextBlock(block.id, { x: Math.max(0, Math.min(256 - origin.width, Math.round(origin.x + dx))), y: Math.max(0, Math.min(256 - origin.height, Math.round(origin.y + dy))) });
      else updateTextBlock(block.id, { width: Math.max(36, Math.min(256 - origin.x, Math.round(origin.width + dx))), height: Math.max(24, Math.min(256 - origin.y, Math.round(origin.height + dy))) });
    };
    const onEnd = () => { window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onEnd); };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onEnd, { once: true });
  }

  async function saveWhiteboard() {
    if (boardReadOnly) return;
    try {
      const response = await fetch('/api/whiteboard', {
        method: 'PATCH', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          title: whiteboard.title, palette: whiteboard.palette, pixelsBase64: bytesToBase64(pixelsRef.current),
          textBlocks: whiteboard.textBlocks, expectedVersion: whiteboard.version,
          changeSummary: '대표가 256×256 그림과 배치 텍스트를 편집',
        }),
      });
      const data = await response.json() as WhiteboardDocument & { error?: string };
      if (!response.ok) throw new Error(data.error ?? '저장 충돌');
      setWhiteboard(data);
      lastSyncedBoardRef.current = data;
      lastSyncedPixelsRef.current = pixelsRef.current.slice();
      setBoardDirty(false);
      setToast(`화이트보드 v${data.version}이 공용 서버에 저장됐습니다.`);
      controlChannel.current?.postMessage({ type: 'world-changed' });
    } catch (error) { setToast(error instanceof Error ? error.message : '화이트보드 저장에 실패했습니다.'); }
  }

  async function openBoardVersion(version: number) {
    if (boardDirtyRef.current && !window.confirm('저장하지 않은 현재 편집을 버리고 과거 버전을 열까요?')) return;
    setBoardVersionLoading(true);
    try {
      const response = await fetch(`/api/whiteboard?version=${version}`, { cache: 'no-store' });
      const data = await response.json() as WhiteboardDocument & { error?: string };
      if (!response.ok || !data.pixelsBase64) throw new Error(data.error ?? '보드 버전을 불러오지 못했습니다.');
      previewPixelsRef.current = base64ToBytes(data.pixelsBase64);
      setWhiteboard(lastSyncedBoardRef.current);
      pixelsRef.current = lastSyncedPixelsRef.current.slice();
      setBoardDirty(false);
      boardPreviewRef.current = true;
      setBoardPreview(data);
      requestAnimationFrame(() => drawBoard(canvasRef.current, previewPixelsRef.current, data.palette));
    } catch (error) {
      setToast(error instanceof Error ? error.message : '보드 버전을 불러오지 못했습니다.');
    } finally {
      setBoardVersionLoading(false);
    }
  }

  function returnToCurrentBoard() {
    boardPreviewRef.current = false;
    setBoardPreview(null);
    requestAnimationFrame(() => drawBoard(canvasRef.current, pixelsRef.current, whiteboard.palette));
  }

  async function loadJobAudit(jobId: string) {
    setJobAuditLoading(jobId);
    try {
      const response = await fetch(`/api/jobs/${jobId}`, { cache: 'no-store' });
      const data = await response.json() as JobAudit & { error?: string };
      if (!response.ok || !data.job) throw new Error(data.error ?? '작업 감사 로그를 불러오지 못했습니다.');
      setJobAudit(data);
    } catch (error) {
      setToast(error instanceof Error ? error.message : '작업 감사 로그를 불러오지 못했습니다.');
    } finally {
      setJobAuditLoading(null);
    }
  }

  if (!selected) {
    return <main className="app-shell loading-world"><div><b>공유 월드에 접속 중</b><span>상시 실행 서버의 최신 좌표와 작업을 불러오고 있습니다.</span></div>{toast && <div className="toast" role="status">{toast}</div>}</main>;
  }

  const rotation = { east: '0deg', south: '90deg', west: '180deg', north: '270deg' }[selected.facing];
  const selectedEvents = selected.events ?? [];
  const activityEvents = selectedEvents.filter((event) => event.type !== 'agent_message').slice().sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const collaborationEvents = selectedEvents.filter((event) => event.type === 'agent_message').slice().reverse();
  const selectedStatusLabel = agentsPaused ? '정지' : selected.activeJob?.status === 'queued' ? '대기' : selected.activeJob?.status === 'active' ? (selected.currentTool ? '작업' : '이동') : selected.activeJob?.status === 'failed' ? '실패' : '대기';

  return (
    <main className="app-shell interactive-shell">
      <header className="topbar">
        <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">C</span><div><strong>CASHCOW HQ</strong><small>{viewer?.displayName ?? '대표님'} · 공유 에이전트 {agents.length}명</small></div></div>
        <div className="world-controls">
          <button className={visionLayer ? 'active' : ''} type="button" onClick={() => setVisionLayer((value) => !value)}>시야 레이어</button>
          <button type="button" onClick={() => setModal('reports')}>리포트 {reports.length}</button>
          {canControlAgents && <button className={`agent-power-toggle ${agentsPaused ? 'paused' : ''}`} type="button" aria-pressed={agentsPaused} aria-busy={controlSaving} onClick={() => void toggleAgents()} disabled={!controlReady || controlSaving || !runtimeOnline}><span aria-hidden="true">{agentsPaused ? '▶' : 'Ⅱ'}</span>{controlSaving ? '저장 중' : agentsPaused ? '에이전트 재개' : '에이전트 정지'}</button>}
          <span className={`world-status ${agentsPaused ? 'paused' : ''} ${!runtimeOnline ? 'offline' : ''}`}><i className="live-dot" /> {!runtimeOnline ? 'RECONNECTING' : agentsPaused ? 'PAUSED' : `LIVE · v${worldVersion}`} <b>{formatTime(clock)}</b></span>
        </div>
        {canControlAgents && <button className={`review-button ${!reviewSubmitted ? 'has-alert' : ''}`} type="button" onClick={() => setModal('review')}>주간 평가 <span>{reviewSubmitted ? '완료' : `${reviewCycle.daysLeft}일 남음`}</span></button>}
      </header>

      <section className="workspace">
        <div className={`world-frame ${agentsPaused ? 'agents-paused' : ''}`} aria-label={`서버에서 계속 실행되는 픽셀 사무실 · 월드 버전 ${worldVersion}`}>
          {agentsPaused && <div className="pause-banner"><b>에이전트 일시정지</b><span>서버 이동 · 작업 · 호출 중단</span></div>}
          {!runtimeOnline && <div className="pause-banner connection-banner"><b>재연결 중</b><span>마지막 서버 상태를 표시하고 있습니다.</span></div>}
          {visionLayer && <div className="vision-cone" style={{ left: `${selected.position.x}%`, top: `${selected.position.y}%`, transform: `translateY(-50%) rotate(${rotation})` }} />}
          <div className="room-label meeting-label">MEETING ROOM</div><div className="room-label lab-label">FOCUS LAB</div>
          <button className="room meeting-room tool-button" type="button" onClick={() => setModal('meeting-room')} aria-label="회의실 열기"><div className="meeting-table"><i /><i /><i /><i /><i /><i /></div><span className="door meeting-door">▾</span></button>
          <button className="room focus-room tool-button" type="button" onClick={() => setModal('computer')} aria-label="컴퓨터 현황 열기">
            <div className="desk-row upper"><span className={`desk pc ${computerOwners[0].owner ? 'busy' : ''}`} /><span className={`desk pc ${computerOwners[1].owner ? 'busy' : ''}`} /></div><div className="desk-row lower"><span className={`desk pc ${computerOwners[2].owner ? 'busy' : ''}`} /><span className={`desk pc ${computerOwners[3].owner ? 'busy' : ''}`} /></div><span className="door lab-door">▾</span>
          </button>
          <div className="lounge-rug"><span /><span /><i /></div>
          <button className="whiteboard-object tool-button" type="button" onClick={() => setModal('whiteboard')} aria-label="화이트보드 열기"><b>{whiteboard.title}</b><span>{whiteboard.authorName} · v{whiteboard.version}</span><em>열기</em></button>
          <div className="plant plant-one" /><div className="plant plant-two" /><div className="window-strip" aria-hidden="true"><i /><i /><i /><i /></div>
          {agents.map((agent) => <PixelAgent key={agent.id} agent={agent} selected={agent.id === selectedId} observed={visibleIds.has(agent.id)} paused={agentsPaused} onSelect={(event) => selectAgent(event, agent.id)} />)}
          <div className="you-marker" style={{ left: `${player.x}%`, top: `${player.y}%` }}><span className={`agent-sprite violet face-${playerFacing}`}><i className="agent-hair" /><i className="agent-face" /><i className="agent-body" /></span><b>대표님</b><i className="hearing-ring" /></div>
          <div className="chat-stream" aria-live="polite">{messages.slice(-3).map((message) => <p key={message.id} className={message.kind}><b>{message.speaker}</b><span>{message.body}</span></p>)}</div>
          <div className="map-hint">서버가 브라우저와 무관하게 계속 실행 · WASD 이동 · 우클릭 업무 감사</div>
          <div className="observation-feed"><b>{selected.name}의 관찰</b>{observationLog.slice(0, 2).map((event) => <span key={event.id}>{event.body}</span>)}</div>
        </div>

        <aside className="ops-panel">
          <div className="panel-heading"><div><small>공유 서버 상태 · 우클릭 상세</small><h2>{selected.name} <span>{selected.role} · {selected.rank}</span></h2></div><span className={`status-pill ${agentsPaused ? 'paused' : selected.currentTool ? 'working' : ''}`}>{selectedStatusLabel}</span></div>
          <div className="agent-card"><div className={`portrait ${selected.tone}`}><span>{selected.name.slice(0, 1)}</span></div><div><b>{selected.focus}</b><p>{selected.activity}</p></div><strong>{Math.round(selected.progress)}%</strong></div>
          <div className="progress-track"><i style={{ width: `${selected.progress}%` }} /></div>
          <div className="agent-metrics"><span><b>{selected.score}</b>성과</span><span><b>{Math.round(distance(player, selected.position))}</b>거리</span><span><b>{canSee(selected, player) ? 'ON' : 'OFF'}</b>대표 시야</span></div>
          <div className="detail-tabs" role="tablist">{(['plan', 'activity', 'messages', 'report', 'memory'] as DetailTab[]).map((tab) => <button key={tab} type="button" role="tab" aria-selected={detailTab === tab} className={detailTab === tab ? 'active' : ''} onClick={() => setDetailTab(tab)}>{({ plan: '플랜', activity: '판단·도구', messages: '협업', report: '리포트', memory: '교훈' })[tab]}</button>)}</div>

          {detailTab === 'plan' && <section className="mini-section detail-content"><div className="section-title"><h3>서버 작업 플랜</h3><span>{selected.plan.filter((step) => step.status === 'done').length}/{selected.plan.length} 완료</span></div>{selected.activeJob && <p className="job-id">작업 {selected.activeJob.id.slice(0, 8)} · {selected.activeJob.phase}</p>}<ol className="plan-list">{selected.plan.map((step, index) => <li key={step.id} className={step.status}><i>{step.status === 'done' ? '✓' : step.status === 'blocked' ? '!' : index + 1}</i><span>{step.title}<small>{step.detail ?? (step.tool ? `${TOOL_LABELS[step.tool]} 필요` : '서버 판단')}</small></span></li>)}</ol>{!selected.plan.length && <p className="empty-state">현재 대기 중인 명령이 없습니다.</p>}</section>}
          {detailTab === 'activity' && <section className="mini-section detail-content activity-timeline"><div className="section-title"><h3>검증 가능한 판단·도구 입출력</h3><span>민감정보 제거됨</span></div>{activityEvents.map((event) => event.type === 'decision' ? <DecisionCard key={event.id} event={event} /> : <article key={event.id} className="event-card"><header><b>{eventLabel(event.type)} · {event.title}</b><time>{formatTime(event.at)}</time></header>{event.tool && <small>도구: {event.tool}</small>}{event.input && <p><span>전달 내용</span>{event.input}</p>}{event.output && <p><span>반환 내용</span>{event.output}</p>}</article>)}{!selectedEvents.length && <p className="empty-state">명령을 받으면 판단 요약과 실제 도구 기록이 여기에 영속 저장됩니다.</p>}</section>}
          {detailTab === 'messages' && <section className="mini-section detail-content message-list"><div className="section-title"><h3>에이전트 간 전달 전문</h3><span>{collaborationEvents.length}건</span></div>{collaborationEvents.map((event) => <article key={event.id}><header><b>{selected.name} → {event.recipientName ?? event.recipientAgentId}</b><time>{formatTime(event.at)}</time></header><p><span>목적</span>{event.title}</p><p><span>전달</span>{event.input}</p><p><span>응답</span>{event.output}</p></article>)}{!collaborationEvents.length && <p className="empty-state">협업이 필요한 작업에서 전달한 내용과 응답이 그대로 표시됩니다.</p>}</section>}
          {detailTab === 'report' && <section className="mini-section detail-content report-panel"><div className="section-title"><h3>최종 결과</h3><button type="button" onClick={() => setModal('reports')}>전체 보관함</button></div>{selected.report ? <article><header><b>{selected.report.title}</b><em>{selected.report.outcome}</em></header><p>{selected.report.summary}</p><pre>{selected.report.body}</pre>{!!selected.report.limitations.length && <small>한계: {selected.report.limitations.join(' · ')}</small>}</article> : <p className="empty-state">작업 완료 후 검증 결과와 영구 리포트가 발행됩니다.</p>}</section>}
          {detailTab === 'memory' && <section className="mini-section detail-content memory-list"><div className="section-title"><h3>완료 후 만든 기억</h3><span>{selected.memories.length}개</span></div>{selected.memories.map((memory) => <article key={memory.id}><b>{memory.kind === 'lesson' ? '교훈' : '경험'}</b><p>{memory.summary}</p><small>근거 {memory.evidence} · 신뢰도 {Math.round(memory.confidence * 100)}%</small></article>)}</section>}
        </aside>
      </section>

      <footer className="chat-dock">
        <button className="nearby" type="button" onClick={() => setModal('notifications')}><span className="sound-icon">◖</span><div><b>근처 대화</b><small>{nearby.length}명이 들을 수 있는 거리</small></div></button>
        <form className="chat-form" onSubmit={submitChat}><label className="chat-input"><span>@</span><input value={draft} onChange={(event) => setDraft(event.target.value)} aria-label="에이전트에게 메시지" placeholder={!runtimeOnline ? '영속 월드 서버에 재연결 중입니다.' : agentsPaused ? '에이전트가 정지되어 호출할 수 없습니다.' : '예: @민지 경쟁사 3곳을 조사하고 완료 리포트를 작성해줘'} maxLength={800} disabled={!controlReady || agentsPaused || !runtimeOnline || commandSending} /><kbd>Enter</kbd></label><button type="submit" className="send-button" disabled={!controlReady || agentsPaused || !runtimeOnline || commandSending}>{commandSending ? '접수 중' : '전송'}</button></form>
      </footer>

      {modal && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !boardDirty) { boardPreviewRef.current = false; setBoardPreview(null); setModal(null); } }}><section className={`modal-card ${modal}`} role="dialog" aria-modal="true" aria-label="도구 상세"><button className="modal-close" type="button" onClick={() => { if (!boardDirty || modal !== 'whiteboard' || window.confirm('저장하지 않은 보드 편집을 닫을까요?')) { boardPreviewRef.current = false; setBoardPreview(null); setBoardDirty(false); setModal(null); } }} aria-label="닫기">×</button>
        {modal === 'whiteboard' && <>
          <header><small>공용 256×256 도구 · v{displayedBoard.version} · {displayedBoard.authorName}</small><h2>화이트보드</h2><p>{boardReadOnly ? '과거 저장 버전을 읽기 전용으로 보고 있습니다.' : '포인터를 드래그해 연속으로 그리고, 텍스트 영역을 같은 보드 위에 배치합니다. 최근 30개 저장 버전은 본문 전체를 다시 열 수 있습니다.'}</p></header>
          <div className={`board-workspace ${boardReadOnly ? 'board-read-only' : ''}`}>
            <div className="board-tools">
              <label>보드 타이틀<input value={displayedBoard.title} maxLength={40} readOnly={boardReadOnly} onChange={(event) => { setWhiteboard((state) => ({ ...state, title: event.target.value })); setBoardDirty(true); }} /></label>
              <div className="palette">{displayedBoard.palette.map((color, index) => <button key={color} className={brushIndex === index ? 'selected' : ''} style={{ background: color }} onClick={() => setBrushIndex(index)} type="button" disabled={boardReadOnly} aria-label={`색상 ${color}`} />)}</div>
              <label>브러시 크기 <output>{brushSize}px</output><input type="range" min="1" max="13" step="2" value={brushSize} disabled={boardReadOnly} onChange={(event) => setBrushSize(Number(event.target.value))} /></label>
              <div className="board-actions">{boardReadOnly
                ? <button type="button" onClick={returnToCurrentBoard}>현재 v{whiteboard.version}로 돌아가기</button>
                : <><button type="button" onClick={() => setBrushIndex(0)}>지우개</button><button type="button" onClick={addTextBlock}>텍스트 추가</button><button type="button" onClick={() => { pixelsRef.current = createBlankBoardPixels(); drawBoard(canvasRef.current, pixelsRef.current, whiteboard.palette); setBoardDirty(true); }}>그림 지우기</button></>}
              </div>
              <div className="board-history"><b>본문 포함 저장 이력</b>{whiteboard.history.map((item) => <button key={item.version} className={item.version === displayedBoard.version ? 'active' : ''} type="button" disabled={boardVersionLoading || item.contentAvailable === false} onClick={() => void openBoardVersion(item.version)}><span>v{item.version} · {item.authorName}</span><small>{item.changeSummary}<br />{item.contentAvailable === false ? '이전 형식 · 본문 없음' : formatTime(item.createdAt)}</small></button>)}</div>
            </div>
            <div className="board-stage"><canvas ref={canvasRef} width="256" height="256" tabIndex={0} aria-label={`256×256 도트 드로잉 캔버스${boardReadOnly ? ' 과거 버전 읽기 전용' : ''}`} onPointerDown={beginPaint} onPointerMove={continuePaint} onPointerUp={endPaint} onPointerCancel={endPaint} />{displayedBoard.textBlocks.map((block) => <div key={block.id} className="board-text-block" style={{ left: `${(block.x / 256) * 100}%`, top: `${(block.y / 256) * 100}%`, width: `${(block.width / 256) * 100}%`, height: `${(block.height / 256) * 100}%`, color: block.color, background: block.background, fontSize: `${Math.max(8, block.fontSize * 1.65)}px` }}><button className="block-move" type="button" disabled={boardReadOnly} onPointerDown={(event) => startBlockGesture(event, block, 'move')} aria-label="텍스트 영역 이동">{block.authorName ?? '작성자'} · {boardReadOnly ? '기록' : '이동'}</button><textarea value={block.text} readOnly={boardReadOnly} aria-label="화이트보드 텍스트 영역" onChange={(event) => updateTextBlock(block.id, { text: event.target.value })} /><button className="block-resize" type="button" disabled={boardReadOnly} onPointerDown={(event) => startBlockGesture(event, block, 'resize')} aria-label="텍스트 영역 크기 조절">↘</button></div>)}</div>
          </div>
          <footer><span>{boardReadOnly ? `과거 v${displayedBoard.version} · ${displayedBoard.authorName} · ${formatTime(displayedBoard.updatedAt)}` : boardDirty ? '저장되지 않은 변경 있음' : `마지막 저장 ${whiteboard.authorName} · ${formatTime(whiteboard.updatedAt)}`}</span>{!boardReadOnly && <button type="button" className="primary-button" onClick={() => void saveWhiteboard()} disabled={!boardDirty}>새 버전으로 공용 저장</button>}</footer>
        </>}
        {modal === 'computer' && <><header><small>서버 점유 · 한 자리당 한 명</small><h2>오피스 컴퓨터</h2><p>Groq의 실제 웹 검색·페이지 방문·코드 인터프리터 결과가 도구 히스토리와 리포트에 남습니다.</p></header><div className="computer-grid">{computerOwners.map(({ station, owner }, index) => <article key={station.id} className={owner ? 'occupied' : ''}><span className="monitor-icon">▣</span><div><b>컴퓨터 {String(index + 1).padStart(2, '0')}</b><small>{owner ? `${owner.name} · ${owner.focus}` : '사용 가능'}</small></div><em>{owner ? '점유 중' : '비어 있음'}</em></article>)}</div><div className="computer-actions"><button type="button" disabled={!runtimeOnline || agentsPaused} onClick={() => { setModal(null); void sendCommand(`@${selected.name} 인터넷에서 최신 근거를 조사하고 출처와 완료 리포트를 정리해줘`); }}>실제 웹 검색 지시</button><button type="button" disabled={!runtimeOnline || agentsPaused} onClick={() => { setModal(null); void sendCommand(`@${selected.name} 안전한 자동화 스크립트를 작성하고 격리 실행으로 결과를 검증해줘`); }}>코드 실행 지시</button><button type="button" disabled={!runtimeOnline || agentsPaused} onClick={() => { setModal(null); void sendCommand(`@${selected.name} 공용 정보에서 관련 기록을 찾아 새 결과를 등록해줘`); }}>공용 정보 지시</button></div></>}
        {modal === 'meeting-room' && <><header><small>물리적 참석 · 서버 좌표</small><h2>협업 회의</h2><p>에이전트들은 각 방에서 회의실 좌석까지 실제로 이동한 뒤, 전달 전문과 응답을 영속 기록합니다.</p></header><div className="attendee-list">{agents.map((agent) => { const seat = preferredMeetingSeat(agent.id); const gap = distance(agent.position, seat.position); return <article key={agent.id}><span className={`mini-avatar ${agent.tone}`}>{agent.name.slice(0,1)}</span><div><b>{agent.name} · {agent.team}</b><small>{gap < 3 ? '회의실 좌석 도착' : `${Math.round(gap)}칸 거리 · ${agent.activity}`}</small></div></article>; })}</div><footer><span>현재 도착 {agents.filter((agent) => distance(agent.position, preferredMeetingSeat(agent.id).position) < 3).length}/4</span><button className="primary-button" type="button" onClick={() => { setModal(null); void sendCommand(`@${selected.name} 회의실에 모두 모여 진행 상황을 공유하고 평가한 뒤 다음 플랜과 리포트를 작성해줘`); }} disabled={!runtimeOnline || agentsPaused}>전원 회의 소집</button></footer></>}
        {modal === 'review' && <><header><small>{reviewCycle.id} 주간 · 대표 전용</small><h2>개인·팀 성과 평가</h2><p>평가가 없으면 다음 주기 시작 시 ‘평가 없음’으로 마감됩니다. 피드백은 서버 기억과 다음 행동 정책에 반영됩니다.</p></header>{reviewSubmitted ? <div className="review-complete"><span>✓</span><h3>이번 주 평가 완료</h3><p>에이전트 서버 기억에 피드백이 저장됐습니다.</p></div> : <div className="review-form"><label>개인 성과 <output>{reviewScores.individual}/5</output><input type="range" min="1" max="5" value={reviewScores.individual} onChange={(event) => setReviewScores((state) => ({ ...state, individual: Number(event.target.value) }))} /></label><label>팀 성과 <output>{reviewScores.team}/5</output><input type="range" min="1" max="5" value={reviewScores.team} onChange={(event) => setReviewScores((state) => ({ ...state, team: Number(event.target.value) }))} /></label><label>행동 피드백<textarea placeholder="예: 결과 공유는 좋았지만 출처를 더 일찍 확인해 주세요." value={reviewScores.comment} onChange={(event) => setReviewScores((state) => ({ ...state, comment: event.target.value }))} /></label><button className="primary-button" type="button" onClick={() => void submitReview()} disabled={!runtimeOnline || agentsPaused}>평가 제출 및 기억 반영</button></div>}</>}
        {modal === 'notifications' && <><header><small>공유 서버 대화 기록</small><h2>대화 로그</h2><p>어느 PC에서 접속해도 동일한 전달 기록을 봅니다.</p></header><div className="message-log">{messages.slice().reverse().map((message) => <article key={message.id}><header><b>{message.speaker}</b><time>{formatTime(message.at)}</time></header><p>{message.body}</p><small>{message.heardBy?.length ? `들은 사람: ${message.heardBy.join(', ')}` : '시스템 이벤트'}{message.planId ? ` · 작업 ${message.planId.slice(0,8)}` : ''}</small></article>)}</div></>}
        {modal === 'reports' && <><header><small>영속 결과 보관함 · {reports.length}건</small><h2>완료 리포트</h2><p>브라우저를 닫아도 서버가 발행한 결과와 당시 판단·도구 입출력이 유지됩니다.</p></header><div className="report-archive">{reports.map((report) => <article key={report.id}><header><div><b>{report.title}</b><small>{agents.find((agent) => agent.id === report.agentId)?.name ?? report.agentId} · {formatTime(report.createdAt)}</small></div><em>{report.outcome}</em></header><p>{report.summary}</p><details><summary>전체 리포트 보기</summary><pre>{report.body}</pre>{!!report.limitations.length && <small>한계: {report.limitations.join(' · ')}</small>}</details><button type="button" onClick={() => void loadJobAudit(report.planId)} disabled={jobAuditLoading === report.planId}>{jobAuditLoading === report.planId ? '감사 로그 로딩' : '당시 플랜·도구 전문 열기'}</button>{jobAudit?.job.id === report.planId && <div className="audit-log"><b>명령: {jobAudit.job.command}</b><small>{jobAudit.job.status} · {jobAudit.job.phase}</small>{jobAudit.job.events.slice().reverse().map((event) => <article key={event.id}>{event.type === 'decision' ? <DecisionCard event={event} /> : <><header><b>{eventLabel(event.type)} · {event.title}</b><time>{formatTime(event.at)}</time></header>{event.input && <p>전달: {event.input}</p>}{event.output && <p>반환: {event.output}</p>}</>}</article>)}</div>}</article>)}{!reports.length && <p className="empty-state">아직 발행된 리포트가 없습니다.</p>}</div></>}
      </section></div>}
      {toast && <div className="toast" role="status">{toast}</div>}
    </main>
  );
}
