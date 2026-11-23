/**
 * 应用外壳：三个页面 —— 落地页 → 初始化 → 编辑器。URL 里放工程 id，刷新 / 前进后退都能回到原处。
 *
 *   /               落地页：选文件、选设置、开始
 *   /?v=<id>        一个工程。它处于哪个阶段决定看到哪个页面：
 *                     uploaded / initializing / failed → 初始化页（终端进度）
 *                     ready                            → 编辑器（带 AI 助手）
 *
 * 这里没有业务状态（旧版 3232 行里的 53 个 useState 都去了 `useProject` 和服务端）。
 * 外壳只管：当前打开哪个工程、落地页的表单、从"上传"到"初始化"的衔接。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as api from './net/client';
import { useProject } from './hooks/useProject';
import { separateInBrowser } from './features/separation';
import VideoUpload from './components/VideoUpload';
import InitView from './components/init/InitView';
import EditorPage from './components/editor/EditorPage';
import { AuthGate } from './auth/AuthGate';
import { useAuth } from './auth/AuthProvider';
import { detectBrowserLanguage } from './utils/languages';
import { t } from '@/i18n';
import { emptyInit, reduceInit, type InitState } from './store/init';
import { pageFor } from './store/page';
import type { BackendAvailability, DubbingMode, ProjectSummary, SeparationBackends, SeparationMode } from './net/types';
import { defaultVoiceForLanguage, downloadBrowserVoice } from './features/browserTts';

const PARAM = 'v';

function readId(): string | null {
  return new URLSearchParams(window.location.search).get(PARAM);
}

function writeId(id: string | null, replace = false): void {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set(PARAM, id); else url.searchParams.delete(PARAM);
  (replace ? window.history.replaceState : window.history.pushState).call(window.history, null, '', url);
}

const App: React.FC = () => {
  const auth = useAuth();
  const [videoId, setVideoId] = useState<string | null>(readId);
  // 刚在这个页面选的文件：浏览器端分离时直接用它解码（刷新后没有，分离会改为向服务器取音频）
  const fileRef = useRef<File | undefined>(undefined);

  // —— 落地页表单 ——
  const [pickedName, setPickedName] = useState<string | undefined>();
  const [targetLanguage, setTargetLanguage] = useState(detectBrowserLanguage);
  const [targetAccent, setTargetAccent] = useState('');
  const [dubbingMode, setDubbingMode] = useState<DubbingMode>('remote');
  const [browserVoice, setBrowserVoice] = useState<string | null>(null);
  // 落地页默认开启声音克隆；浏览器分离也默认选中 In-browser（克隆需要分离后的人声轨）。
  const [enableVoiceClone, setEnableVoiceClone] = useState(true);
  const [cloneSmartPick, setCloneSmartPick] = useState(false);
  // 落地页默认开启 omni 审听增强（纠错 + 语气标签）；用户仍可在处理设置里关掉。
  const [mmEnhance, setMmEnhance] = useState(true);
  const [customPrompt, setCustomPrompt] = useState('');
  const [separationMode, setSeparationMode] = useState<SeparationMode>('client');
  const changeDubbingMode = useCallback((mode: DubbingMode) => {
    setDubbingMode(mode);
    if (mode === 'remote' && separationMode === 'off') setSeparationMode('client');
  }, [separationMode]);
  const [backends, setBackends] = useState<SeparationBackends>({});
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [recent, setRecent] = useState<ProjectSummary[]>([]);

  const project = useProject(auth.loading || !auth.user ? null : videoId);
  const { project: view, loadError, notFound } = project;
  // start() 在 open() 之前创建，它闭包里的 project 还是"没有工程"的那一份；
  // 要在 setup 之后取最新视图，必须经过 ref 拿当前这一帧的 reload。
  const reloadRef = useRef(project.reload);
  reloadRef.current = project.reload;

  // 浏览器端分离的进度（在服务端初始化之前发生，所以不走事件流）
  const [prep, setPrep] = useState<InitState>(emptyInit);
  const [preparing, setPreparing] = useState(false);

  // —— 前进/后退 ——
  useEffect(() => {
    const onPop = () => setVideoId(readId());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  // —— 服务端能力 / 最近工程 ——
  useEffect(() => {
    api.getSeparatorInfo().then((info) => {
      setBackends(info.backends as SeparationBackends);
      const usable = (info.backends as Record<string, BackendAvailability | undefined>).client?.available;
      const on = info.default_enabled && usable;
      setSeparationMode(on ? 'client' : 'off');
      // 克隆需要分离后的人声轨：浏览器后端不可用 / 服务端没开时，别把默认状态留成会失败的 client + clone。
      if (!on) setEnableVoiceClone(false);
    }).catch(() => {
      setSeparationMode('off');
      setEnableVoiceClone(false);
    });
  }, []);

  const refreshProjects = useCallback(() => {
    if (!auth.user) {
      setRecent([]);
      return;
    }
    api.listProjects()
      .then((r) => setRecent(r.projects))
      .catch(() => setRecent([]));
  }, [auth.user]);

  useEffect(() => {
    if (!auth.user) {
      setRecent([]);
    } else if (!videoId) {
      refreshProjects();
    }
  }, [auth.user, videoId, refreshProjects]);

  const open = useCallback((id: string | null, replace = false) => {
    writeId(id, replace);
    setVideoId(id);
  }, []);

  // 只有"工程本身 404"才回落地页。以前用错误文案匹配（/不存在|not found/），
  // 任何一个可选接口返回 Not Found 都会把正在初始化的用户踢回首页 —— 而后端
  // 的 job 还在跑、日志还在刷进度。
  useEffect(() => {
    if (notFound && videoId) {
      fileRef.current = undefined;
      open(null, true);
    }
  }, [notFound, videoId, open]);

  // —— 开始：上传 → （浏览器分离）→ setup ——
  const start = useCallback(async () => {
    if (!auth.user) {
      auth.login();
      return;
    }
    const file = fileRef.current;
    if (!file) return;
    setStarting(true);
    setStartError(null);
    setPrep(emptyInit());
    let uploadedId: string | null = null;
    try {
      const uploaded = await api.upload(file);
      uploadedId = uploaded.id;
      // 马上切到初始化页：浏览器端人声分离可能要几分钟，这段时间用户要看到步骤清单、
      // 进度和取消，而不是停在落地页。别因此闪出旧编辑器 —— pageFor 在 starting /
      // preparing 期间强制停在初始化页（上传按 MD5 复用时工程可能是已完成的）。
      open(uploaded.id);

      let mode: SeparationMode = dubbingMode !== 'off' ? separationMode : 'off';
      if (dubbingMode === 'remote') mode = 'client';
      if (mode === 'client') {
        setPreparing(true);
        setPrep((s) => reduceInit(s, { type: 'progress', seq: 0, ts: 0, phase: 'separation', status: 'started' }));
        const outcome = await separateInBrowser(uploaded.id, file, {
          onLog: (m) => setPrep((s) => ({ ...s, log: [...s.log, { key: `sep-${s.log.length}`, text: m, level: 'info' as const }] })),
          onProgress: (key, text) => setPrep((s) => {
            const i = s.log.findIndex((l) => l.key === key);
            const line = { key, text, level: 'info' as const };
            return { ...s, log: i >= 0 ? s.log.map((l, j) => (j === i ? line : l)) : [...s.log, line] };
          }),
        });
        setPreparing(false);
        // 没分离成功就以“不分离”继续（导出时用原声垫底），而不是让整个初始化失败
        if (!outcome.ok) mode = 'off';
        setPrep((s) => reduceInit(s, { type: 'progress', seq: 0, ts: 0, phase: 'separation', status: outcome.ok ? 'done' : 'skipped' }));
      }

      await api.setup(uploaded.id, {
        target_language: targetLanguage,
        accent: targetLanguage === 'Chinese' ? targetAccent || null : null,
        separation_mode: mode,
        dubbing_mode: dubbingMode,
        browser_voice: dubbingMode === 'browser' ? browserVoice : null,
        enable_voice_clone: dubbingMode === 'remote' && enableVoiceClone,
        clone_smart_pick: dubbingMode === 'remote' && enableVoiceClone && cloneSmartPick,
        mm_enhance: mmEnhance,
        custom_prompt: customPrompt,
      });

      // Browser Piper model download belongs to the init's TTS stage. The
      // server init keeps running ASR/translation while this downloads; the
      // editor fill step then finds the model cached.
      if (dubbingMode === 'browser') {
        const voice = browserVoice ?? defaultVoiceForLanguage(targetLanguage);
        if (voice) {
          const key = 'browser-tts-model';
          const put = (text: string) => setPrep((s) => {
            const i = s.log.findIndex((l) => l.key === key);
            const line = { key, text, level: 'info' as const };
            return { ...s, log: i >= 0 ? s.log.map((l, j) => (j === i ? line : l)) : [...s.log, line] };
          });
          put(`浏览器配音模型：${voice}`);
          try {
            await downloadBrowserVoice(voice, (loaded, total) => {
              const pct = total ? Math.round((loaded / total) * 100) : 0;
              put(`浏览器配音模型 ${pct}%（${(loaded / 1048576).toFixed(1)}/${(total / 1048576).toFixed(1)} MB）`);
            });
            put('浏览器配音模型已缓存');
          } catch (e) {
            put(`浏览器配音模型下载失败：${e instanceof Error ? e.message : String(e)}`);
          }
        }
      }
    } catch (e) {
      setPreparing(false);
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      // setup 返回时服务端已把 stage 改成 initializing，但这个变化经事件流到达之前，
      // 本地视图可能还是旧的 ready。先取一次最新视图再放开 starting，否则放开的
      // 那一瞬间会闪出编辑器。
      if (uploadedId) await reloadRef.current();
      setStarting(false);
    }
  }, [auth.user, auth.login, targetLanguage, targetAccent, dubbingMode, browserVoice, enableVoiceClone, cloneSmartPick, mmEnhance, customPrompt, separationMode, open]);

  const deleteProject = useCallback(async (id: string, filename: string) => {
    if (!window.confirm(t("删除「{0}」？服务器上的所有数据都会被清除，无法恢复。", [filename || id.slice(0, 8)]))) return;
    try {
      await api.deleteProject(id);
      refreshProjects();
    } catch (e) {
      window.alert(t("删除失败：{0}", [e instanceof Error ? e.message : String(e)]));
    }
  }, [refreshProjects]);

  // 初始化页的日志 = 浏览器分离阶段的日志 + 服务端事件流推来的日志
  const initState = useMemo<InitState>(() => {
    const server = project.init;
    const steps = server.steps.map((s) => (s.id === 'separation' ? prep.steps.find((p) => p.id === 'separation') ?? s : s));
    return { ...server, steps, log: [...prep.log, ...server.log] };
  }, [project.init, prep]);

  const job = view?.jobs.find((j) => j.kind === 'init');
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!job && !preparing) return;
    const t0 = Date.now();
    const timer = setInterval(() => setElapsed((Date.now() - t0) / 1000 + (job?.elapsedS ?? 0)), 1000);
    return () => clearInterval(timer);
  }, [job?.id, preparing]);

  // —— 页面 ——
  if (auth.loading) {
    return (
      <div className="min-h-screen bg-claude-bg flex items-center justify-center text-sm text-gray-400">
        {t('加载中…')}
      </div>
    );
  }
  if (!auth.user && videoId) return <AuthGate />;

  const page = pageFor({ videoId, stage: view?.stage ?? null, starting, preparing });
  if (page === 'landing') {
    return (
      <>
      <VideoUpload
        onFilePicked={(f) => { fileRef.current = f; setPickedName(f.name); setStartError(null); }}
        pendingFileName={pickedName}
        onStart={() => void start()}
        isLoading={starting}
        targetLanguage={targetLanguage}
        onLanguageChange={setTargetLanguage}
        targetAccent={targetAccent}
        onAccentChange={setTargetAccent}
        enableVoiceClone={enableVoiceClone}
        onVoiceCloneChange={setEnableVoiceClone}
        mmEnhance={mmEnhance}
        onMmEnhanceChange={setMmEnhance}
        cloneSmartPick={cloneSmartPick}
        onCloneSmartPickChange={setCloneSmartPick}
        dubbingMode={dubbingMode}
        onDubbingModeChange={changeDubbingMode}
        browserVoice={browserVoice}
        onBrowserVoiceChange={setBrowserVoice}
        customPrompt={customPrompt}
        onCustomPromptChange={setCustomPrompt}
        separationMode={separationMode}
        onSeparationModeChange={setSeparationMode}
        separationBackends={backends}
        bgmSeparationLocked={dubbingMode === 'remote'}
        recentProjects={recent}
        onOpenProject={(id) => open(id)}
        onDeleteProject={(id, name) => void deleteProject(id, name)}
      />
      {/* 只覆盖"还没拿到工程 id"的那一段：上传中，或上传本身失败。
          拿到 id 之后就切到初始化页了，分离和启动的进度、失败都在那里显示。 */}
      {(starting || startError) && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 w-[min(560px,90vw)] bg-white border border-claude-border rounded-2xl shadow-xl px-5 py-4 space-y-1.5">
          <div className="text-xs font-bold text-gray-700">{startError ? '没能开始处理' : '正在上传…'}</div>
          {startError && <div className="text-[12px] text-red-500">{startError}</div>}
        </div>
      )}
      </>
    );
  }

  if (page === 'loading' || !view) {
    return (
      <div className="min-h-screen bg-claude-bg flex items-center justify-center text-sm text-gray-400">
        {loadError ? (
          <div className="text-center space-y-3">
            <p className="text-red-500">{loadError}</p>
            <div className="flex items-center justify-center gap-2">
              <button onClick={() => void project.reload()} className="px-4 py-2 rounded-xl bg-claude-accent text-white text-xs font-bold hover:bg-claude-accentHover">重试</button>
              <button onClick={() => open(null)} className="px-4 py-2 rounded-xl border border-claude-border text-xs font-bold text-gray-500 hover:bg-white">返回</button>
            </div>
          </div>
        ) : '加载中…'}
      </div>
    );
  }

  if (page === 'init') {
    const running = view.stage === 'initializing' || preparing || starting;
    return (
      <>
        <InitView
          filename={view.filename}
          init={initState}
          error={view.stage === 'failed' ? view.initError : startError}
          currentStep={job?.step ?? (preparing ? '人声分离' : null)}
          elapsedS={elapsed}
          cancelling={job?.cancelling}
          onCancel={() => void project.run('cancel_job')}
          onRetry={() => { setStartError(null); void project.run('run_initialize'); }}
          onEnter={() => void project.reload()}
          onBack={() => open(null)}
        />
        {!running && view.stage === 'uploaded' && !startError && (
          <div className="fixed bottom-6 left-1/2 -translate-x-1/2 bg-white border border-claude-border rounded-2xl shadow-lg px-5 py-3 text-xs text-gray-500 flex items-center gap-4">
            这个工程还没开始处理。
            <button onClick={() => void project.run('run_initialize')} className="px-3 py-1.5 rounded-lg bg-claude-accent text-white font-bold hover:bg-claude-accentHover">开始</button>
          </div>
        )}
      </>
    );
  }

  return (
    <>
      <EditorPage project={project} onHome={() => open(null)} />
      {/* 对已完成的工程重新点「开始处理」时，setup 可能在 stage=ready 上就失败；
          初始化页不会出现，这条错误也得有地方显示。 */}
      {startError && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 w-[min(520px,90vw)] bg-white border border-red-200 rounded-2xl shadow-xl px-5 py-3 flex items-center gap-4 text-xs">
          <span className="text-red-500 flex-grow">{startError}</span>
          <button onClick={() => setStartError(null)} className="px-3 py-1.5 rounded-lg border border-claude-border text-gray-500 font-bold hover:bg-white">关闭</button>
        </div>
      )}
    </>
  );
};

export default App;
