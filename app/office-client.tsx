'use client';

import { FormEvent, MouseEvent as ReactMouseEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  AgentModel,
  COMPUTER_STATIONS,
  HEARING_RADIUS,
  MEETING_SEATS,
  Memory,
  TOOL_SPOTS,
  buildCommandPlan,
  canAcquireExclusiveTool,
  canSee,
  directionFromDelta,
  distance,
  hearingRecipients,
  kstReviewCycle,
  movePoint,
  navigateToward,
  reachedAssignedTool,
  targetToolFromCommand,
  transitionVisibility,
} from '@/lib/world';

type Viewer = { displayName: string; email: string } | null;
type DetailTab = 'plan' | 'history' | 'memory';
type ModalName = 'whiteboard' | 'computer' | 'meeting-room' | 'review' | 'notifications' | null;
type ChatMessage = { id: string; speaker: string; body: string; at: string; kind: 'user' | 'agent' | 'system'; heardBy?: string[] };
type WhiteboardState = { title: string; text: string; pixels: string[]; version: number };

const PIXEL_COLORS = ['#f9f4df', '#ef7657', '#e4b34f', '#55a47c', '#4d8fb8', '#745f9d', '#26394f'];
const DEFAULT_PIXELS = Array.from({ length: 160 }, (_, index) => {
  const x = index % 16;
  const y = Math.floor(index / 16);
  if ((x === 3 || x === 12) && y > 1 && y < 8) return '#ef7657';
  if (y === 2 && x > 3 && x < 12) return '#e4b34f';
  if (y === 7 && x > 3 && x < 12) return '#55a47c';
  if ((x === 7 || x === 8) && y > 3 && y < 7) return '#4d8fb8';
  return '#f9f4df';
});

const iso = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();

function starterPlan(prefix: string, tool: 'whiteboard' | 'computer' | 'meeting-room', titles: string[]) {
  return titles.map((title, index) => ({
    id: `${prefix}-${index}`,
    title,
    detail: index === 1 ? '도착 후 도구 점유를 확인합니다.' : undefined,
    status: (index === 0 ? 'done' : index === 1 ? 'active' : 'pending') as 'done' | 'active' | 'pending',
    tool: index === 1 || index === 2 ? tool : undefined,
  }));
}

const INITIAL_AGENTS: AgentModel[] = [
  {
    id: 'minji', name: '민지', team: '전략팀', role: '프로젝트 매니저', rank: '리드', tone: 'coral',
    position: { x: 31, y: 36 }, facing: 'east', target: TOOL_SPOTS.whiteboard,
    activity: '화이트보드로 이동 중', focus: '시장 조사 플랜 정리', progress: 34, score: 91, visible: true,
    plan: starterPlan('minji', 'whiteboard', ['대표 요청과 완료 조건 확인', '화이트보드로 이동', '조사 결과를 구조화', '팀에게 공유하고 교훈 저장']),
    history: [
      { id: 'mh1', at: iso(11), tool: '컴퓨터 02 · 인터넷 검색', input: 'AI 협업 공간 사례', output: '공신력 있는 자료 12건과 비교 항목 5개', result: 'success' },
      { id: 'mh2', at: iso(18), tool: '공용 정보', input: '리서치 노트 #24', output: '근거 URL과 함께 등록 완료', result: 'success' },
    ],
    memories: [
      { id: 'mm1', kind: 'lesson', at: iso(60), summary: '조사 전에 비교 기준을 합의하면 재작업이 줄어든다.', evidence: 'run_024 · 재작업 2회 → 0회', confidence: .86 },
      { id: 'mm2', kind: 'episode', at: iso(180), summary: '지난 회의에서 출처 없는 수치는 채택되지 않았다.', evidence: 'meeting_018', confidence: .94 },
    ],
  },
  {
    id: 'doyun', name: '도윤', team: '리서치팀', role: '리서처', rank: '시니어', tone: 'blue',
    position: { x: 52, y: 35 }, facing: 'east', target: COMPUTER_STATIONS[0].position, toolStation: 'pc-01',
    activity: '컴퓨터 03으로 이동 중', focus: '경쟁 서비스 조사', progress: 58, score: 88, visible: true,
    plan: starterPlan('doyun', 'computer', ['질문을 검색 가능한 단위로 분해', '빈 컴퓨터로 이동', '웹 자료 조사 및 교차 검증', '공용 정보에 출처와 함께 등록']),
    history: [{ id: 'dh1', at: iso(7), tool: '컴퓨터 01 · 공용 정보', input: '최근 사용자 인터뷰', output: '인터뷰 요약 8건 조회', result: 'success' }],
    memories: [{ id: 'dm1', kind: 'lesson', at: iso(240), summary: '서로 다른 출처 두 곳에서 일치한 사실만 결론에 사용한다.', evidence: 'review_2026w33', confidence: .91 }],
  },
  {
    id: 'harin', name: '하린', team: '제품팀', role: '프로덕트 디자이너', rank: '주니어', tone: 'gold',
    position: { x: 64, y: 60 }, facing: 'south', target: TOOL_SPOTS.whiteboard,
    activity: '보드 사용 순서 대기', focus: '도트 UI 흐름 시각화', progress: 22, score: 83, visible: true,
    plan: starterPlan('harin', 'whiteboard', ['요구사항에서 핵심 장면 추출', '화이트보드로 이동', '16×10 도트 흐름도 작성', '사용성 피드백 반영']),
    history: [{ id: 'hh1', at: iso(26), tool: '화이트보드 · PIXEL FLOW', input: '초기 화면 3안', output: '16×10 도트 스케치 저장', result: 'success' }],
    memories: [{ id: 'hm1', kind: 'episode', at: iso(420), summary: '작은 화면에서는 상태 색만으로 의미가 전달되지 않았다.', evidence: 'usability_006', confidence: .78 }],
  },
  {
    id: 'jun', name: '준', team: '플랫폼팀', role: '소프트웨어 엔지니어', rank: '시니어', tone: 'mint',
    position: { x: 44, y: 69 }, facing: 'north', target: COMPUTER_STATIONS[3].position, toolStation: 'pc-04',
    activity: '자동화 스크립트 설계 중', focus: '공용 지식 동기화', progress: 73, score: 94, visible: true,
    plan: starterPlan('jun', 'computer', ['입출력과 실패 조건 정의', '격리 실행용 컴퓨터로 이동', '스크립트 작성 및 결과 검증', '실패 원인과 재사용 규칙 저장']),
    history: [{ id: 'jh1', at: iso(4), tool: '컴퓨터 04 · 스크립트', input: '지식 중복 탐지', output: '검사 41건 · 중복 후보 3건', result: 'working' }],
    memories: [{ id: 'jm1', kind: 'lesson', at: iso(300), summary: '외부 입력은 실행 전에 스키마와 크기 제한을 모두 검사한다.', evidence: 'incident_003', confidence: .97 }],
  },
];

const TOOL_LABELS = { whiteboard: '화이트보드', computer: '컴퓨터', 'meeting-room': '회의실' } as const;
const NEXT_TOOLS: Record<string, Array<keyof typeof TOOL_SPOTS>> = {
  minji: ['whiteboard', 'meeting-room', 'computer'],
  doyun: ['computer', 'whiteboard', 'meeting-room'],
  harin: ['whiteboard', 'computer', 'meeting-room'],
  jun: ['computer', 'meeting-room', 'whiteboard'],
};

function formatTime(value: string | Date) {
  return new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul' }).format(new Date(value));
}

function safeInitialDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed;
}

function preferredComputer(agentId: string, offset = 0) {
  const base = { minji: 1, doyun: 0, harin: 2, jun: 3 }[agentId as 'minji' | 'doyun' | 'harin' | 'jun'] ?? 0;
  return COMPUTER_STATIONS[(base + offset) % COMPUTER_STATIONS.length];
}

function preferredMeetingSeat(agentId: string) {
  return MEETING_SEATS.find((seat) => seat.id === `seat-${agentId}`) ?? MEETING_SEATS[0];
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

export default function OfficeClient({ viewer, canControlAgents, initialNow }: { viewer: Viewer; canControlAgents: boolean; initialNow: string }) {
  const [agents, setAgents] = useState(INITIAL_AGENTS);
  const [selectedId, setSelectedId] = useState('minji');
  const [detailTab, setDetailTab] = useState<DetailTab>('plan');
  const [player, setPlayer] = useState({ x: 49, y: 54 });
  const [playerFacing, setPlayerFacing] = useState<'north' | 'east' | 'south' | 'west'>('south');
  const [visionLayer, setVisionLayer] = useState(true);
  const [agentsPaused, setAgentsPaused] = useState(false);
  const [controlReady, setControlReady] = useState(false);
  const [controlSaving, setControlSaving] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: 'hello', speaker: '민지', body: '대표님, 이번 스프린트 조사 기준을 먼저 정리하고 있어요.', at: iso(3), kind: 'agent', heardBy: ['대표님'] },
  ]);
  const [draft, setDraft] = useState('');
  const [modal, setModal] = useState<ModalName>(null);
  const [toast, setToast] = useState('');
  const [clock, setClock] = useState(() => safeInitialDate(initialNow));
  const [color, setColor] = useState(PIXEL_COLORS[1]);
  const [whiteboard, setWhiteboard] = useState<WhiteboardState>({ title: 'SPRINT 08', text: '목표 · 진행 · 배운 점', pixels: DEFAULT_PIXELS, version: 1 });
  const [reviewScores, setReviewScores] = useState({ individual: 4, team: 4, comment: '' });
  const [reviewSubmitted, setReviewSubmitted] = useState(false);
  const [observationLog, setObservationLog] = useState<Array<{ id: string; body: string; at: string }>>([]);
  const keys = useRef(new Set<string>());
  const previousVisible = useRef(new Set<string>());
  const agentsPausedRef = useRef(false);
  const agentRequests = useRef(new Set<AbortController>());
  const reviewRequest = useRef<AbortController | null>(null);
  const controlReadSequence = useRef(0);
  const controlSavingRef = useRef(false);
  const controlChannel = useRef<BroadcastChannel | null>(null);

  const selected = agents.find((agent) => agent.id === selectedId) ?? agents[0];
  const reviewCycle = useMemo(() => kstReviewCycle(clock), [clock]);
  const visibleIds = useMemo(() => {
    if (!visionLayer) return new Set(agents.map((agent) => agent.id));
    return new Set(agents.filter((agent) => agent.id === selected.id || canSee(selected, agent.position)).map((agent) => agent.id));
  }, [agents, selected, visionLayer]);
  const nearby = useMemo(() => hearingRecipients(player, agents), [player, agents]);
  const computerOwners = COMPUTER_STATIONS.map((station) => ({ station, owner: agents.find((agent) => agent.currentTool === 'computer' && agent.toolStation === station.id) }));

  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const sequence = ++controlReadSequence.current;
    fetch('/api/bootstrap')
      .then(async (response) => {
        if (!response.ok) throw new Error('bootstrap unavailable');
        return response.json() as Promise<{ whiteboard?: WhiteboardState; reviewSubmitted?: boolean; agentsPaused?: boolean }>;
      })
      .then((data: { whiteboard?: WhiteboardState; reviewSubmitted?: boolean; agentsPaused?: boolean }) => {
        if (sequence !== controlReadSequence.current) return;
        if (data.whiteboard) setWhiteboard({ ...data.whiteboard, pixels: data.whiteboard.pixels.length === 160 ? data.whiteboard.pixels : DEFAULT_PIXELS });
        setReviewSubmitted(Boolean(data.reviewSubmitted));
        agentsPausedRef.current = Boolean(data.agentsPaused);
        setAgentsPaused(Boolean(data.agentsPaused));
      })
      .catch(() => {
        if (sequence !== controlReadSequence.current) return;
        agentsPausedRef.current = true;
        setAgentsPaused(true);
        setToast('활동 상태를 확인하지 못해 안전을 위해 에이전트를 정지했습니다.');
      })
      .finally(() => { if (sequence === controlReadSequence.current) setControlReady(true); });
  }, []);

  useEffect(() => {
    let disposed = false;
    const syncControl = async () => {
      if (controlSavingRef.current) return;
      const sequence = ++controlReadSequence.current;
      try {
        const response = await fetch('/api/world-control', { cache: 'no-store' });
        if (!response.ok) throw new Error('control unavailable');
        const data = await response.json() as { agentsPaused?: unknown };
        if (typeof data.agentsPaused !== 'boolean') throw new Error('invalid control state');
        if (disposed || sequence !== controlReadSequence.current) return;
        agentsPausedRef.current = data.agentsPaused;
        setAgentsPaused(data.agentsPaused);
        if (data.agentsPaused) {
          agentRequests.current.forEach((controller) => controller.abort());
          agentRequests.current.clear();
          reviewRequest.current?.abort();
          reviewRequest.current = null;
        }
        setControlReady(true);
      } catch {
        if (disposed || sequence !== controlReadSequence.current) return;
        agentsPausedRef.current = true;
        setAgentsPaused(true);
        agentRequests.current.forEach((controller) => controller.abort());
        agentRequests.current.clear();
        reviewRequest.current?.abort();
        reviewRequest.current = null;
        setControlReady(true);
        setToast('활동 상태 동기화에 실패해 안전을 위해 에이전트를 정지했습니다.');
      }
    };
    const syncWhenVisible = () => { if (document.visibilityState === 'visible') void syncControl(); };
    const timer = window.setInterval(syncWhenVisible, 30_000);
    window.addEventListener('focus', syncWhenVisible);
    document.addEventListener('visibilitychange', syncWhenVisible);
    if ('BroadcastChannel' in window) {
      const channel = new BroadcastChannel('cashcow-agent-control');
      controlChannel.current = channel;
      channel.addEventListener('message', (event: MessageEvent<{ type?: unknown }>) => {
        if (event.data?.type === 'world-control-changed') void syncControl();
      });
    }
    return () => {
      disposed = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', syncWhenVisible);
      document.removeEventListener('visibilitychange', syncWhenVisible);
      controlChannel.current?.close();
      controlChannel.current = null;
    };
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(''), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => () => {
    agentRequests.current.forEach((controller) => controller.abort());
    agentRequests.current.clear();
    reviewRequest.current?.abort();
    reviewRequest.current = null;
  }, []);

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
        setPlayer((current) => movePoint(current, dx, dy));
        setPlayerFacing((current) => directionFromDelta(dx, dy, current));
      }
    }, 42);
    return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); window.clearInterval(mover); };
  }, []);

  useEffect(() => {
    if (!controlReady || agentsPaused) return;
    const simulation = window.setInterval(() => {
      if (agentsPausedRef.current) return;
      setAgents((currentAgents) => {
        if (agentsPausedRef.current) return currentAgents;
        const claimedComputers = new Map(currentAgents.filter((agent) => agent.currentTool === 'computer' && agent.toolStation).map((agent) => [agent.toolStation!, agent.id]));
        return currentAgents.map((agent) => {
          if (agent.currentTool) {
            const progress = Math.min(100, agent.progress + 1.25);
            if (progress < 100) return { ...agent, progress, activity: `${TOOL_LABELS[agent.currentTool]}에서 작업 중` };

            if (agent.currentTool === 'computer' && agent.toolStation) claimedComputers.delete(agent.toolStation);
            const completedTool = agent.currentTool;
            const completedAt = new Date().toISOString();
            const sequence = NEXT_TOOLS[agent.id];
            const nextTool = sequence[(agent.history.length + 1) % sequence.length];
            const lesson: Memory = {
              id: crypto.randomUUID(), kind: 'lesson', at: completedAt,
              summary: `${TOOL_LABELS[completedTool]} 작업은 입력과 완료 조건을 먼저 확인할 때 안정적이었다.`,
              evidence: `run_${agent.id}_${Date.now()}`, confidence: .72,
            };
            const nextStation = nextTool === 'computer' ? preferredComputer(agent.id, agent.history.length) : nextTool === 'meeting-room' ? preferredMeetingSeat(agent.id) : undefined;
            return {
              ...agent,
              currentTool: undefined,
              progress: 8,
              target: nextStation?.position ?? TOOL_SPOTS[nextTool],
              toolStation: nextStation?.id,
              activity: `${TOOL_LABELS[nextTool]}(으)로 이동 중`,
              focus: `${TOOL_LABELS[nextTool]} 기반 자율 개선`,
              plan: buildCommandPlan(`${TOOL_LABELS[nextTool]}에서 다음 개선 작업 수행`, nextTool),
              history: [{ id: crypto.randomUUID(), at: completedAt, tool: TOOL_LABELS[completedTool], input: agent.focus, output: '검증된 산출물을 공용 기록에 반영', result: 'success' as const }, ...agent.history].slice(0, 12),
              memories: [lesson, ...agent.memories].slice(0, 10),
              score: Math.min(100, agent.score + 1),
            };
          }

          const navigation = navigateToward(agent.position, agent.target, .72);
          const reached = reachedAssignedTool(agent, navigation.point);
          if (!reached) {
            return {
              ...agent,
              position: navigation.point,
              facing: navigation.facing,
              activity: `${TOOL_LABELS[targetToolFromCommand(agent.focus)]}(으)로 이동 중`,
              plan: agent.plan.map((step, index) => ({ ...step, status: index === 0 ? 'done' : index === 1 ? 'active' : 'pending' })),
            };
          }

          const reachedTool = reached.tool;
          if (reachedTool === 'computer' && !canAcquireExclusiveTool(agent.id, claimedComputers.get(reached.stationId!))) {
            const alternate = COMPUTER_STATIONS.find((station) => !claimedComputers.has(station.id));
            if (alternate) return { ...agent, position: navigation.point, facing: navigation.facing, target: alternate.position, toolStation: alternate.id, activity: `${alternate.id.toUpperCase()}로 재이동 중`, plan: agent.plan.map((step, index) => index === 1 ? { ...step, status: 'active' } : step) };
            const ownerId = claimedComputers.get(reached.stationId!);
            return { ...agent, position: navigation.point, facing: navigation.facing, activity: `컴퓨터 대기 · ${currentAgents.find((item) => item.id === ownerId)?.name} 사용 중`, progress: Math.max(6, agent.progress - .25), plan: agent.plan.map((step, index) => index === 1 ? { ...step, status: 'blocked' } : step) };
          }
          if (reachedTool === 'computer') claimedComputers.set(reached.stationId!, agent.id);
          return {
            ...agent,
            position: navigation.point,
            facing: navigation.facing,
            currentTool: reachedTool,
            toolStation: reached.stationId,
            activity: `${TOOL_LABELS[reachedTool]} 사용 시작`,
            progress: Math.max(agent.progress, 12),
            plan: agent.plan.map((step, index) => ({ ...step, status: index <= 1 ? 'done' : index === 2 ? 'active' : 'pending' })),
            history: [{ id: crypto.randomUUID(), at: new Date().toISOString(), tool: `${TOOL_LABELS[reachedTool]} 점유`, input: '도구 사용 가능 여부 확인', output: '점유 승인 · 물리적 도착 검증', result: 'working' as const }, ...agent.history].slice(0, 12),
          };
        });
      });
    }, 180);
    return () => window.clearInterval(simulation);
  }, [agentsPaused, controlReady]);

  useEffect(() => {
    const changes = transitionVisibility(previousVisible.current, visibleIds);
    if (changes.length) {
      setObservationLog((current) => [
        ...changes.map((change) => ({
          id: crypto.randomUUID(),
          body: change.type === 'seen'
            ? `${agents.find((agent) => agent.id === change.id)?.name ?? '캐릭터'} 발견 · 활동 상태 확인`
            : `${agents.find((agent) => agent.id === change.id)?.name ?? '캐릭터'} 시야에서 사라짐`,
          at: new Date().toISOString(),
        })),
        ...current,
      ].slice(0, 8));
    }
    previousVisible.current = new Set(visibleIds);
  }, [visibleIds, agents]);

  function selectAgent(event: ReactMouseEvent, id: string) {
    event.preventDefault();
    setSelectedId(id);
    setDetailTab('plan');
  }

  function assignCommand(agentIds: string[], command: string) {
    if (!controlReady || agentsPausedRef.current) return;
    const tool = targetToolFromCommand(command);
    setAgents((current) => {
      const reserved = new Set(current.filter((agent) => agent.currentTool === 'computer' && !agentIds.includes(agent.id)).map((agent) => agent.toolStation).filter(Boolean));
      return current.map((agent) => {
        if (!agentIds.includes(agent.id)) return agent;
        const station = tool === 'computer' ? (COMPUTER_STATIONS.find((item) => !reserved.has(item.id)) ?? preferredComputer(agent.id)) : tool === 'meeting-room' ? preferredMeetingSeat(agent.id) : undefined;
        if (station) reserved.add(station.id);
        return {
          ...agent,
          focus: command.replace(/@[가-힣\w-]+/g, '').trim().slice(0, 62) || '대표 요청 수행',
          target: station?.position ?? TOOL_SPOTS[tool],
          toolStation: station?.id,
          currentTool: undefined,
          activity: '대표 요청 확인 중',
          progress: 5,
          plan: buildCommandPlan(command, tool),
          history: [{ id: crypto.randomUUID(), at: new Date().toISOString(), tool: '근거리 채팅', input: command, output: `${TOOL_LABELS[tool]} 작업으로 분류`, result: 'success' as const }, ...agent.history].slice(0, 12),
        };
      });
    });
  }

  async function sendCommand(messageOverride?: string) {
    if (!controlReady || agentsPausedRef.current) {
      setToast('에이전트가 일시정지 상태입니다. 재개한 뒤 명령해 주세요.');
      return;
    }
    const body = (messageOverride ?? draft).trim();
    if (!body) return;
    const mention = agents.find((agent) => body.includes(`@${agent.name}`));
    const heard = hearingRecipients(player, agents, HEARING_RADIUS);
    const recipients = mention ? heard.filter((agent) => agent.id === mention.id) : heard;
    const at = new Date().toISOString();
    setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: '대표님', body, at, kind: 'user' as const, heardBy: recipients.map((agent) => agent.name) }].slice(-20));
    setDraft('');

    if (mention && !recipients.length) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: '시스템', body: `${mention.name}은(는) ${Math.round(distance(player, mention.position))}칸 떨어져 있어 듣지 못했습니다. 가까이 이동해 주세요.`, at, kind: 'system' as const }].slice(-20));
      return;
    }
    if (!recipients.length) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: '시스템', body: '현재 대화를 들을 수 있는 에이전트가 없습니다.', at, kind: 'system' as const }].slice(-20));
      return;
    }

    assignCommand(recipients.map((agent) => agent.id), body);
    const primary = mention ?? recipients[0];
    setSelectedId(primary.id);
    setDetailTab('plan');
    setToast(`${recipients.map((agent) => agent.name).join(', ')}에게 명령이 전달됐습니다.`);
    const controller = new AbortController();
    agentRequests.current.add(controller);
    try {
      const response = await fetch('/api/agent', { method: 'POST', headers: { 'content-type': 'application/json' }, signal: controller.signal, body: JSON.stringify({ agentId: primary.id, agentName: primary.name, role: primary.role, rank: primary.rank, command: body, nearbyAgents: recipients.map((agent) => agent.name), memories: primary.memories.slice(0, 3) }) });
      const data = await response.json() as { reply?: string; plan?: string[]; lesson?: string; error?: string };
      if (agentsPausedRef.current) return;
      const reply = data.reply ?? (data.error ? `요청은 접수했지만 사고 엔진 연결이 지연되고 있어요. ${data.error}` : '요청을 접수했고 도구로 이동할게요.');
      setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: primary.name, body: reply, at: new Date().toISOString(), kind: 'agent' as const, heardBy: ['대표님'] }].slice(-20));
      if (data.plan?.length || data.lesson) {
        setAgents((current) => current.map((agent) => agent.id === primary.id ? {
          ...agent,
          plan: data.plan?.length ? data.plan.slice(0, 5).map((title, index) => ({ id: crypto.randomUUID(), title, status: index === 0 ? 'active' : 'pending', tool: index === 1 || index === 2 ? targetToolFromCommand(body) : undefined })) : agent.plan,
          memories: data.lesson ? [{ id: crypto.randomUUID(), kind: 'lesson' as const, at: new Date().toISOString(), summary: data.lesson, evidence: `groq_plan_${Date.now()}`, confidence: .75 }, ...agent.memories].slice(0, 10) : agent.memories,
        } : agent));
      }
    } catch {
      if (controller.signal.aborted || agentsPausedRef.current) return;
      setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: primary.name, body: '네, 요청을 접수했어요. 도구로 이동해 작업을 시작할게요.', at: new Date().toISOString(), kind: 'agent' as const, heardBy: ['대표님'] }].slice(-20));
    } finally {
      agentRequests.current.delete(controller);
    }
  }

  function submitChat(event: FormEvent) { event.preventDefault(); void sendCommand(); }

  async function saveWhiteboard() {
    try {
      const response = await fetch('/api/whiteboard', { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...whiteboard, expectedVersion: whiteboard.version }) });
      const data = await response.json() as WhiteboardState & { error?: string };
      if (!response.ok) throw new Error(data.error ?? '저장 충돌');
      setWhiteboard(data);
      setToast('화이트보드 제목·글·도트가 공용 보드에 저장됐습니다.');
      setModal(null);
    } catch (error) {
      setToast(error instanceof Error ? error.message : '화이트보드 저장에 실패했습니다.');
    }
  }

  function startMeeting() {
    if (!controlReady || agentsPausedRef.current) {
      setToast('에이전트가 일시정지 상태입니다. 재개한 뒤 회의를 소집해 주세요.');
      return;
    }
    const command = '회의실에 모여 진행 상황을 공유하고 결과를 평가한 뒤 다음 플랜을 조정해줘';
    assignCommand(agents.map((agent) => agent.id), command);
    setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: '시스템', body: '전원 회의가 소집됐습니다. 에이전트들이 순간이동 없이 회의실로 이동합니다.', at: new Date().toISOString(), kind: 'system' as const }].slice(-20));
    setModal(null);
    setToast('회의 소집 · 4명이 물리적으로 도착하면 시작됩니다.');
  }

  async function toggleAgents() {
    if (!canControlAgents || !controlReady || controlSaving) return;
    const previousPaused = agentsPausedRef.current;
    const nextPaused = !previousPaused;
    if (nextPaused) {
      agentsPausedRef.current = true;
      setAgentsPaused(true);
      agentRequests.current.forEach((controller) => controller.abort());
      agentRequests.current.clear();
      reviewRequest.current?.abort();
      reviewRequest.current = null;
    }
    controlSavingRef.current = true;
    controlReadSequence.current += 1;
    setControlSaving(true);
    try {
      const response = await fetch('/api/world-control', {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ agentsPaused: nextPaused }),
      });
      const data = await response.json() as { agentsPaused?: boolean; error?: string };
      if (!response.ok || typeof data.agentsPaused !== 'boolean') throw new Error(data.error ?? '활동 상태를 저장하지 못했습니다.');
      agentsPausedRef.current = data.agentsPaused;
      setAgentsPaused(data.agentsPaused);
      controlChannel.current?.postMessage({ type: 'world-control-changed' });
      const body = data.agentsPaused
        ? '대표가 에이전트 호출과 모든 이동·작업을 일시정지했습니다.'
        : '대표가 에이전트 활동을 재개했습니다. 에이전트들이 다시 이동하고 작업을 시작합니다.';
      setMessages((current) => [...current, { id: crypto.randomUUID(), speaker: '시스템', body, at: new Date().toISOString(), kind: 'system' as const }].slice(-20));
      setToast(data.agentsPaused ? '에이전트 정지 · 이동, 작업, 호출이 멈췄습니다.' : '에이전트 재개 · 이동과 작업을 다시 시작합니다.');
    } catch (error) {
      const safePaused = nextPaused || previousPaused;
      agentsPausedRef.current = safePaused;
      setAgentsPaused(safePaused);
      const message = error instanceof Error ? error.message : '활동 상태를 저장하지 못했습니다.';
      setToast(nextPaused ? `${message} 현재 화면은 정지 상태로 유지됩니다.` : message);
    } finally {
      controlSavingRef.current = false;
      setControlSaving(false);
    }
  }

  async function submitReview() {
    if (!controlReady || agentsPausedRef.current) {
      setToast('에이전트가 일시정지 상태입니다. 재개한 뒤 평가를 반영해 주세요.');
      return;
    }
    const controller = new AbortController();
    reviewRequest.current = controller;
    try {
      const response = await fetch('/api/reviews', { method: 'POST', headers: { 'content-type': 'application/json' }, signal: controller.signal, body: JSON.stringify({ cycleId: reviewCycle.id, ...reviewScores, agentIds: agents.map((agent) => agent.id) }) });
      if (!response.ok) throw new Error('평가를 저장하지 못했습니다.');
      if (controller.signal.aborted || agentsPausedRef.current) return;
      const lessonText = reviewScores.comment.trim() || `개인 ${reviewScores.individual}점, 팀 ${reviewScores.team}점 평가를 다음 작업 정책에 반영한다.`;
      setAgents((current) => current.map((agent) => ({ ...agent, score: Math.round((agent.score * .8) + ((reviewScores.individual + reviewScores.team) * 10) * .2), memories: [{ id: crypto.randomUUID(), kind: 'lesson' as const, at: new Date().toISOString(), summary: lessonText, evidence: `weekly_review_${reviewCycle.id}`, confidence: .95 }, ...agent.memories].slice(0, 10) })));
      setReviewSubmitted(true);
      setModal(null);
      setToast('주간 평가가 저장됐고 에이전트들이 행동을 교훈으로 전환했습니다.');
    } catch (error) {
      if (!controller.signal.aborted) setToast(error instanceof Error ? error.message : '평가 저장에 실패했습니다.');
    } finally {
      if (reviewRequest.current === controller) reviewRequest.current = null;
    }
  }

  function paintPixel(index: number) {
    setWhiteboard((current) => ({ ...current, pixels: current.pixels.map((pixel, pixelIndex) => pixelIndex === index ? color : pixel) }));
  }

  const rotation = { east: '0deg', south: '90deg', west: '180deg', north: '270deg' }[selected.facing];

  return (
    <main className="app-shell interactive-shell">
      <header className="topbar">
        <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">C</span><div><strong>CASHCOW HQ</strong><small>{viewer?.displayName ?? '대표님'} · 에이전트 {agents.length}명 근무 중</small></div></div>
        <div className="world-controls">
          <button className={visionLayer ? 'active' : ''} type="button" onClick={() => setVisionLayer((value) => !value)}>시야 레이어</button>
          {canControlAgents && <button className={`agent-power-toggle ${agentsPaused ? 'paused' : ''}`} type="button" aria-pressed={agentsPaused} aria-busy={controlSaving} aria-label={agentsPaused ? '에이전트 활동 재개' : '에이전트 활동 정지'} onClick={() => void toggleAgents()} disabled={!controlReady || controlSaving}><span aria-hidden="true">{agentsPaused ? '▶' : 'Ⅱ'}</span>{controlSaving ? '저장 중' : agentsPaused ? '에이전트 재개' : '에이전트 정지'}</button>}
          <span className={`world-status ${agentsPaused ? 'paused' : ''}`}><i className="live-dot" /> {!controlReady ? 'SYNC' : agentsPaused ? 'PAUSED' : 'LIVE'} <b>{formatTime(clock)}</b></span>
        </div>
        <button className={`review-button ${!reviewSubmitted ? 'has-alert' : ''}`} type="button" onClick={() => setModal('review')}>주간 평가 <span>{reviewSubmitted ? '완료' : `${reviewCycle.daysLeft}일 남음`}</span></button>
      </header>

      <section className="workspace">
        <div className={`world-frame ${agentsPaused ? 'agents-paused' : ''}`} aria-label={`위에서 내려다본 픽셀 사무실 · 에이전트 ${agentsPaused ? '일시정지' : '활동 중'}`}>
          {agentsPaused && <div className="pause-banner"><b>에이전트 일시정지</b><span>이동 · 작업 · 호출 중단</span></div>}
          {visionLayer && <div className="vision-cone" style={{ left: `${selected.position.x}%`, top: `${selected.position.y}%`, transform: `translateY(-50%) rotate(${rotation})` }} />}
          <div className="room-label meeting-label">MEETING ROOM</div><div className="room-label lab-label">FOCUS LAB</div>
          <button className="room meeting-room tool-button" type="button" onClick={() => setModal('meeting-room')} aria-label="회의실 열기"><div className="meeting-table"><i /><i /><i /><i /><i /><i /></div><span className="door meeting-door">▾</span></button>
          <button className="room focus-room tool-button" type="button" onClick={() => setModal('computer')} aria-label="컴퓨터 현황 열기">
            <div className="desk-row upper"><span className={`desk pc ${computerOwners[0].owner ? 'busy' : ''}`} /><span className={`desk pc ${computerOwners[1].owner ? 'busy' : ''}`} /></div><div className="desk-row lower"><span className={`desk pc ${computerOwners[2].owner ? 'busy' : ''}`} /><span className={`desk pc ${computerOwners[3].owner ? 'busy' : ''}`} /></div><span className="door lab-door">▾</span>
          </button>
          <div className="lounge-rug"><span /><span /><i /></div>
          <button className="whiteboard-object tool-button" type="button" onClick={() => setModal('whiteboard')} aria-label="화이트보드 열기"><b>{whiteboard.title}</b><span>{whiteboard.text}</span><em>열기</em></button>
          <div className="plant plant-one" /><div className="plant plant-two" /><div className="window-strip" aria-hidden="true"><i /><i /><i /><i /></div>
          {agents.map((agent) => <PixelAgent key={agent.id} agent={agent} selected={agent.id === selectedId} observed={visibleIds.has(agent.id)} paused={agentsPaused} onSelect={(event) => selectAgent(event, agent.id)} />)}
          <div className="you-marker" style={{ left: `${player.x}%`, top: `${player.y}%` }}><span className={`agent-sprite violet face-${playerFacing}`}><i className="agent-hair" /><i className="agent-face" /><i className="agent-body" /></span><b>대표님</b><i className="hearing-ring" /></div>
          <div className="chat-stream" aria-live="polite">{messages.slice(-3).map((message) => <p key={message.id} className={message.kind}><b>{message.speaker}</b><span>{message.body}</span></p>)}</div>
          <div className="map-hint">{agentsPaused ? '에이전트 정지 중 · 대표 이동과 업무 열람은 계속 가능' : 'WASD / 방향키 이동 · 우클릭 업무 보기 · 가구를 클릭해 도구 열기'}</div>
          <div className="observation-feed"><b>{selected.name}의 관찰</b>{observationLog.slice(0, 2).map((event) => <span key={event.id}>{event.body}</span>)}</div>
        </div>

        <aside className="ops-panel">
          <div className="panel-heading"><div><small>선택한 에이전트 · 우클릭 상세</small><h2>{selected.name} <span>{selected.role} · {selected.rank}</span></h2></div><span className={`status-pill ${agentsPaused ? 'paused' : selected.currentTool ? 'working' : ''}`}>{agentsPaused ? '정지' : selected.currentTool ? '작업' : '이동'}</span></div>
          <div className="agent-card"><div className={`portrait ${selected.tone}`}><span>{selected.name.slice(0, 1)}</span></div><div><b>{selected.focus}</b><p>{agentsPaused ? `일시정지 · ${selected.activity}` : selected.activity}</p></div><strong>{Math.round(selected.progress)}%</strong></div>
          <div className="progress-track"><i style={{ width: `${selected.progress}%` }} /></div>
          <div className="agent-metrics"><span><b>{selected.score}</b>성과</span><span><b>{Math.round(distance(player, selected.position))}</b>거리</span><span><b>{canSee(selected, player) ? 'ON' : 'OFF'}</b>대표 시야</span></div>
          <div className="detail-tabs" role="tablist">{(['plan', 'history', 'memory'] as DetailTab[]).map((tab) => <button key={tab} type="button" role="tab" aria-selected={detailTab === tab} className={detailTab === tab ? 'active' : ''} onClick={() => setDetailTab(tab)}>{tab === 'plan' ? '플랜' : tab === 'history' ? '도구 기록' : '기억·교훈'}</button>)}</div>

          {detailTab === 'plan' && <section className="mini-section detail-content"><div className="section-title"><h3>현재 플랜</h3><span>{selected.plan.filter((step) => step.status === 'done').length}/{selected.plan.length} 완료</span></div><ol className="plan-list">{selected.plan.map((step, index) => <li key={step.id} className={step.status}><i>{step.status === 'done' ? '✓' : step.status === 'blocked' ? '!' : index + 1}</i><span>{step.title}<small>{step.detail ?? (step.tool ? `${TOOL_LABELS[step.tool]} 필요` : '에이전트 자체 판단')}</small></span></li>)}</ol></section>}
          {detailTab === 'history' && <section className="mini-section detail-content tool-history"><div className="section-title"><h3>도구 입출력 히스토리</h3><span>민감정보 제거됨</span></div>{selected.history.map((event) => <article key={event.id}><header><b>{event.tool}</b><time>{formatTime(event.at)}</time></header><p><span>입력</span>{event.input}</p><p><span>출력</span>{event.output}</p><em className={event.result}>{event.result === 'success' ? '성공' : event.result === 'working' ? '진행 중' : '대기'}</em></article>)}</section>}
          {detailTab === 'memory' && <section className="mini-section detail-content memory-list"><div className="section-title"><h3>경험에서 만든 기억</h3><span>{selected.memories.length}개</span></div>{selected.memories.map((memory) => <article key={memory.id}><b>{memory.kind === 'lesson' ? '교훈' : '경험'}</b><p>{memory.summary}</p><small>근거 {memory.evidence} · 신뢰도 {Math.round(memory.confidence * 100)}%</small></article>)}</section>}
        </aside>
      </section>

      <footer className="chat-dock">
        <button className="nearby" type="button" onClick={() => setModal('notifications')}><span className="sound-icon">◖</span><div><b>근처 대화</b><small>{nearby.length}명이 들을 수 있는 거리</small></div></button>
        <form className="chat-form" onSubmit={submitChat}><label className="chat-input"><span>@</span><input value={draft} onChange={(event) => setDraft(event.target.value)} aria-label="에이전트에게 메시지" placeholder={agentsPaused ? '에이전트가 정지되어 호출할 수 없습니다.' : '예: @민지 경쟁사 3곳을 조사해서 보드에 정리해줘'} maxLength={800} disabled={!controlReady || agentsPaused} /><kbd>Enter</kbd></label><button type="submit" className="send-button" disabled={!controlReady || agentsPaused}>전송</button></form>
      </footer>

      {modal && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setModal(null); }}><section className={`modal-card ${modal}`} role="dialog" aria-modal="true" aria-label="도구 상세"><button className="modal-close" type="button" onClick={() => setModal(null)} aria-label="닫기">×</button>
        {modal === 'whiteboard' && <><header><small>공용 도구 · 버전 {whiteboard.version}</small><h2>화이트보드</h2><p>제목으로 용도를 알리고 글 영역과 16×10 도트 영역을 함께 편집합니다.</p></header><div className="board-editor"><div className="board-copy"><label>보드 타이틀<input value={whiteboard.title} maxLength={30} onChange={(event) => setWhiteboard((state) => ({ ...state, title: event.target.value }))} /></label><label>글 영역<textarea value={whiteboard.text} maxLength={400} onChange={(event) => setWhiteboard((state) => ({ ...state, text: event.target.value }))} /></label><div className="palette">{PIXEL_COLORS.map((item) => <button key={item} className={color === item ? 'selected' : ''} style={{ background: item }} onClick={() => setColor(item)} type="button" aria-label={`색상 ${item}`} />)}</div></div><div className="pixel-canvas">{whiteboard.pixels.map((pixel, index) => <button key={index} type="button" onPointerDown={() => paintPixel(index)} style={{ background: pixel }} aria-label={`도트 ${index + 1}`} />)}</div></div><footer><button type="button" className="secondary-button" onClick={() => setWhiteboard((state) => ({ ...state, pixels: Array(160).fill(PIXEL_COLORS[0]) }))}>도트 지우기</button><button type="button" className="primary-button" onClick={() => void saveWhiteboard()}>공용 보드에 저장</button></footer></>}
        {modal === 'computer' && <><header><small>한 자리당 한 명만 사용</small><h2>오피스 컴퓨터</h2><p>검색·스크립트·공용 정보 작업은 에이전트가 컴퓨터까지 이동해 점유한 뒤 실행합니다.</p></header><div className="computer-grid">{computerOwners.map(({ station, owner }, index) => <article key={station.id} className={owner ? 'occupied' : ''}><span className="monitor-icon">▣</span><div><b>컴퓨터 {String(index + 1).padStart(2, '0')}</b><small>{owner ? `${owner.name} · ${owner.focus}` : '사용 가능'}</small></div><em>{owner ? '점유 중' : '비어 있음'}</em></article>)}</div><div className="computer-actions"><button type="button" disabled={!controlReady || agentsPaused} onClick={() => { setModal(null); void sendCommand(`@${selected.name} 인터넷에서 최신 근거를 조사하고 출처를 정리해줘`); }}>인터넷 검색 지시</button><button type="button" disabled={!controlReady || agentsPaused} onClick={() => { setModal(null); void sendCommand(`@${selected.name} 안전한 자동화 스크립트를 작성하고 결과를 검증해줘`); }}>스크립트 작성 지시</button><button type="button" disabled={!controlReady || agentsPaused} onClick={() => { setModal(null); void sendCommand(`@${selected.name} 공용 정보에서 관련 기록을 찾아 새 결과를 등록해줘`); }}>공용 정보 지시</button></div></>}
        {modal === 'meeting-room' && <><header><small>물리적 참석 · 정족수 3명</small><h2>주간 조정 회의</h2><p>도착한 에이전트들이 진행 상황을 공유하고 서로 평가한 뒤 플랜을 조정합니다.</p></header><div className="attendee-list">{agents.map((agent) => { const seat = preferredMeetingSeat(agent.id); const gap = distance(agent.position, seat.position); return <article key={agent.id}><span className={`mini-avatar ${agent.tone}`}>{agent.name.slice(0,1)}</span><div><b>{agent.name} · {agent.team}</b><small>{gap < 3 ? '회의실 좌석 도착' : `${Math.round(gap)}칸 거리 · ${agent.activity}`}</small></div></article>; })}</div><footer><span>현재 도착 {agents.filter((agent) => distance(agent.position, preferredMeetingSeat(agent.id).position) < 3).length}/4</span><button className="primary-button" type="button" onClick={startMeeting} disabled={!controlReady || agentsPaused}>전원 회의 소집</button></footer></>}
        {modal === 'review' && <><header><small>{reviewCycle.id} 주간 · 대표 전용</small><h2>개인·팀 성과 평가</h2><p>평가가 없으면 다음 주기 시작 시 ‘평가 없음’으로 마감됩니다. 점수와 피드백은 에이전트의 다음 행동 정책에 반영됩니다.</p></header>{reviewSubmitted ? <div className="review-complete"><span>✓</span><h3>이번 주 평가 완료</h3><p>에이전트들은 피드백과 이전 행동을 근거로 새 교훈을 만들었습니다.</p></div> : <div className="review-form"><label>개인 성과 <output>{reviewScores.individual}/5</output><input type="range" min="1" max="5" value={reviewScores.individual} onChange={(event) => setReviewScores((state) => ({ ...state, individual: Number(event.target.value) }))} /></label><label>팀 성과 <output>{reviewScores.team}/5</output><input type="range" min="1" max="5" value={reviewScores.team} onChange={(event) => setReviewScores((state) => ({ ...state, team: Number(event.target.value) }))} /></label><label>행동 피드백<textarea placeholder="예: 결과 공유는 좋았지만 출처를 더 일찍 확인해 주세요." value={reviewScores.comment} onChange={(event) => setReviewScores((state) => ({ ...state, comment: event.target.value }))} /></label><button className="primary-button" type="button" onClick={() => void submitReview()} disabled={!controlReady || agentsPaused}>평가 제출 및 회고 시작</button></div>}</>}
        {modal === 'notifications' && <><header><small>거리 기반 전달 기록</small><h2>대화와 관찰 로그</h2><p>채팅은 발화 시점에 {HEARING_RADIUS}칸 안에 있던 에이전트에게만 전달됩니다.</p></header><div className="message-log">{messages.slice().reverse().map((message) => <article key={message.id}><header><b>{message.speaker}</b><time>{formatTime(message.at)}</time></header><p>{message.body}</p><small>{message.heardBy?.length ? `들은 사람: ${message.heardBy.join(', ')}` : '시스템 이벤트'}</small></article>)}</div></>}
      </section></div>}
      {toast && <div className="toast" role="status">{toast}</div>}
    </main>
  );
}
