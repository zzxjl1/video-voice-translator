import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { t, tBackend } from '@/i18n';
import type { CloneVoiceView, CommandResult, Line, SpeakerView, VoicePresetOption } from '../../net/types';
import type { HeaderProps } from '../../hooks/useWindows';
import { billing as billingApi } from '../../net/client';

/**
 * 音色面板：一次只看一个说话人，听几个克隆音色念同一段固定的话，挑一个用。
 *
 * 界面三块：顶部切换说话人 → 那段固定的试听句 → 取材方式列表。
 * 每种取材方式直接列成一行：做过的可以试听 / 使用，没做的有一个「克隆」按钮，做不了的写明原因。
 * 费用只写在点击「克隆」后的确认框里，不摆在界面上。手动指定参考句这类少用的操作留给聊天里的 AI 助手。
 * 试听句是固定的（不是这个工程里的某句译文）：换了工程、没翻译完都能对比。试听文件没有时，点播放才合成。
 *
 * 为什么按说话人而不是按句：克隆音色是说话人级资源，一个人说到一半换声音也不自然。
 * 单句的特殊情况走脚本里每句菜单的"这句用别的音色"。
 */

type Run = <T = Record<string, unknown>>(name: string, args?: Record<string, unknown>) => Promise<CommandResult<T>>;

interface Props {
  speakers: SpeakerView[];
  lines: Line[];
  colorOf: (speakerId: string) => string;
  dubbing: boolean;
  focusSpeaker: string | null;
  run: Run;
  onNotice: (message: string) => void;
  docked?: boolean;
  dragProps?: HeaderProps;
  windowControls?: React.ReactNode;
}

const CARD = 'flex-grow flex flex-col h-full bg-[#fbfbf9] border border-[#e5e5e0] rounded-3xl overflow-hidden shadow-sm';
const CARD_FLOATING = 'flex-grow flex flex-col h-full bg-[#fbfbf9] overflow-hidden';
const CARD_HEAD = 'px-6 py-5 border-b border-[#e5e5e0] flex items-center justify-between bg-white/60 backdrop-blur-md';
const CARD_TITLE = 'text-xs font-serif font-bold text-gray-700 flex items-center gap-2 tracking-wide';

/**
 * 每个新建音色的价格（元，含服务费）。**从接口取**：写死在代码里会和后台调价脱节。
 * 取不到时用 null，确认框就不显示金额（宁可不说，也不报错价）。
 */
let clonePricePromise: Promise<number | null> | null = null;
function clonePrice(): Promise<number | null> {
  if (!clonePricePromise) {
    clonePricePromise = billingApi.prices()
      .then((p) => p.per_unit.find((x) => x.step === 'clone')?.price_yuan ?? null)
      .catch((): number | null => { clonePricePromise = null; return null; });
  }
  return clonePricePromise;
}

/** 面板共用一个播放器：点另一个就停掉上一个，不会几个音色叠在一起响。 */
function useSharedPlayer() {
  const ref = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  useEffect(() => {
    const a = new Audio();
    ref.current = a;
    const stop = () => setPlaying(null);
    a.addEventListener('ended', stop);
    return () => { a.pause(); a.removeEventListener('ended', stop); };
  }, []);
  const play = useCallback((key: string, url: string) => {
    const a = ref.current;
    if (!a) return;
    a.src = url;
    void a.play().then(() => setPlaying(key)).catch(() => setPlaying(null));
  }, []);
  const stop = useCallback(() => { ref.current?.pause(); setPlaying(null); }, []);
  return { playing, play, stop };
}

const VoicePanel: React.FC<Props> = ({
  speakers, lines, colorOf, dubbing, focusSpeaker, run, onNotice,
  docked = true, dragProps, windowControls,
}) => {
  const player = useSharedPlayer();
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => { if (focusSpeaker) setSelected(focusSpeaker); }, [focusSpeaker]);
  const speaker = speakers.find((s) => s.id === selected) ?? speakers[0];

  return (
    <div className={docked ? CARD : CARD_FLOATING}>
      <div {...dragProps} className={`${CARD_HEAD} shrink-0 ${dragProps?.className ?? ''}`}>
        <h3 className={CARD_TITLE}>
          <span className="w-1.5 h-1.5 bg-claude-accent rounded-full"></span>
          音色
        </h3>
        {windowControls}
      </div>

      {!dubbing ? (
        <p className="p-6 text-[13px] text-gray-400 leading-relaxed">这个项目是「仅字幕」模式，不生成配音，不需要音色。</p>
      ) : !speaker ? (
        <p className="p-6 text-[13px] text-gray-400">还没有说话人。</p>
      ) : (
        <>
          {speakers.length > 1 && (
            <div className="shrink-0 px-5 pt-4 flex gap-1.5 flex-wrap">
              {speakers.map((s) => {
                const on = s.id === speaker.id;
                return (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => { player.stop(); setSelected(s.id); }}
                    className={[
                      'flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-[12px] font-bold transition-colors',
                      on ? 'bg-white border-claude-accent text-claude-accent shadow-sm' : 'bg-transparent border-transparent text-gray-500 hover:bg-white/70',
                    ].join(' ')}
                  >
                    <span className="w-2 h-2 rounded-full" style={{ backgroundColor: colorOf(s.id) }} />
                    {s.name}
                  </button>
                );
              })}
            </div>
          )}
          <SpeakerVoices
            key={speaker.id}
            speaker={speaker}
            lines={lines}
            player={player}
            run={run}
            onNotice={onNotice}
          />
        </>
      )}
    </div>
  );
};

/** 一行 = 一种取材方式。v 为空 = 还没克隆。 */
interface Row {
  key: string;
  name: string;
  preset: string | null;
  option: VoicePresetOption | null;
  v: CloneVoiceView | null;
  current: boolean;
}
type MadeRow = Row & { v: CloneVoiceView };

interface VoicesProps {
  speaker: SpeakerView;
  lines: Line[];
  player: ReturnType<typeof useSharedPlayer>;
  run: Run;
  onNotice: (m: string) => void;
}

const rangeText = (ls: number[], seconds: number | null) =>
  ls.length ? `取自第 ${ls.join('、')} 句${seconds ? ` · ${seconds} 秒` : ''}` : '';

const SpeakerVoices: React.FC<VoicesProps> = ({ speaker, lines, player, run, onNotice }) => {
  const [busy, setBusy] = useState<string | null>(null);
  // 点了播放但试听还没合成：合成好、地址随状态推来之后自动播
  const [wantPlay, setWantPlay] = useState<string | null>(null);

  const { currentClone, candidates, previewText, presetOptions, material } = speaker;

  // 每种取材方式一行；不属于任何取材方式的音色（手动指定的、旧版留下的）排在最后。
  const rows = useMemo<Row[]>(() => {
    const voices: { v: CloneVoiceView; current: boolean }[] = [
      ...(currentClone ? [{ v: currentClone, current: true }] : []),
      ...candidates.map((v) => ({ v, current: false })),
    ];
    const taken = new Set<number>();
    const out: Row[] = presetOptions.map((o) => {
      const at = voices.findIndex((x, i) => !taken.has(i) && x.v.preset === o.id);
      if (at >= 0) taken.add(at);
      return { key: o.id, name: o.label, preset: o.id, option: o, v: at >= 0 ? voices[at].v : null, current: at >= 0 && voices[at].current };
    });
    voices.forEach((x, i) => {
      if (taken.has(i)) return;
      out.push({ key: x.v.id ?? `x${i}`, name: x.v.presetLabel ?? '原来的音色', preset: null, option: null, v: x.v, current: x.current });
    });
    return out;
  }, [currentClone, candidates, presetOptions]);

  const translated = useMemo(
    () => lines.filter((l) => l.speakerId === speaker.id && l.kind === 'speech' && l.translation.trim()),
    [lines, speaker.id],
  );
  const thin = material.total < 10 || material.longest < 5;

  useEffect(() => {
    if (!wantPlay) return;
    const row = rows.find((r) => r.key === wantPlay);
    if (row?.v?.previewUrl) { player.play(`v-${wantPlay}`, row.v.previewUrl); setWantPlay(null); }
  }, [wantPlay, rows, player]);

  const act = useCallback(async (key: string, fn: () => Promise<CommandResult>, fail: string) => {
    setBusy(key);
    try {
      const r = await fn();
      if (!r.ok) onNotice(r.error ? tBackend(r.error) : t(fail));
      return r;
    } finally {
      setBusy(null);
    }
  }, [onNotice]);

  const listen = async (row: MadeRow) => {
    if (player.playing === `v-${row.key}`) { player.stop(); return; }
    if (row.v.previewUrl) { player.play(`v-${row.key}`, row.v.previewUrl); return; }
    setWantPlay(row.key);
    const r = await act('prev', () => run<{ preview_url?: string | null }>('preview_voice_candidates', {
      speaker: speaker.id,
      ...(row.v.voiceId ? { voice_id: row.v.voiceId } : {}),
    }), '合成试听失败');
    if (!r.ok) { setWantPlay(null); return; }
    // 试听文件是这次请求刚写的，视图里的 previewUrl 可能还是 null（没有状态变更）；
    // 直接用接口返回的地址播放，避免“点一次没反应，刷新后才好用”。
    const url = (r.result as { preview_url?: string | null } | undefined)?.preview_url;
    if (url) { player.play(`v-${row.key}`, url); setWantPlay(null); }
    else setWantPlay(null);
  };

  // 克隆这一种取材方式：花钱，所以点了才问；费用只写在确认框里
  const clone = async (row: Row) => {
    if (!row.preset) return;
    const price = await clonePrice();
    const money = price == null ? '' : t("\n约 {0} 元（含服务费）", [price.toFixed(2)]);
    if (!window.confirm(t("用「{0}」克隆 {1} 的音色？要几十秒，做好后可以直接试听。", [t(row.name), speaker.name]) + money)) return;
    await act(`gen-${row.key}`, () => run('generate_voice_candidates', { speaker: speaker.id, presets: [row.preset] }), '克隆失败');
  };

  // 默认克隆：和初始化完全一样（clone_voice 不带参数）——素材够就用中句拼接，不够退到连读 / 单句；
  // 开了智能选材就先让 AI 挑。做完直接成为这个人当前的音色。
  const hasMaterial = presetOptions.some((o) => o.id !== 'smart' && o.available);
  const cloneDefault = async () => {
    const price = await clonePrice();
    const money = price == null ? '' : t("\n约 {0} 元（含服务费），从余额扣。", [price.toFixed(2)]);
    if (!window.confirm(t("克隆 {0} 的音色？要几十秒。", [speaker.name]) + money)) return;
    const r = await act('gen-default', () => run('clone_voice', { speaker: speaker.id }), '克隆失败');
    if (!r.ok) return;
    const todo = translated.filter((l) => !l.muted).map((l) => l.line);
    if (!todo.length) return;
    // 和换音色一样：克隆不花配音的钱，重新配音才花；问清楚，取消就先保持现有配音
    const go = window.confirm(
      t("{0} 的克隆音色做好了。\n{1} 句配音要用新音色重新合成，会产生 TTS 费用。\n\n现在重新配音吗？\n（选“取消”则先保持现有配音，这些句子会标为「需更新」，之后点顶栏的「补」。）", [speaker.name, todo.length]),
    );
    if (go) await act('redub', () => run('redub', { lines: todo }), '重新配音失败');
  };

  const use = async (row: MadeRow) => {
    if (!row.v.id) return;
    const r = await act(`use-${row.v.id}`, () => run('use_voice_candidate', { speaker: speaker.id, candidate: row.v.id }), '换用失败');
    if (!r.ok) return;
    const todo = translated.filter((l) => !l.muted).map((l) => l.line);
    if (!todo.length) return;
    // 换音色不花钱，重新配音才花钱：先问清楚。取消 = 保持现有配音，那些句子标成「需更新」。
    const go = window.confirm(
      t("已换成「{0}」。\n{1} 的 {2} 句配音要用新音色重新合成，会产生 TTS 费用。\n\n现在重新配音吗？\n（选“取消”则先保持现有配音，这些句子会标为「需更新」，之后点顶栏的「补」。）", [t(row.name), speaker.name, todo.length]),
    );
    if (go) await act('redub', () => run('redub', { lines: todo }), '重新配音失败');
  };

  const clean = async () => {
    if (!window.confirm(t("删掉 {0} 没在用的 {1} 个音色？\n服务端的也会一起删除，无法找回。", [speaker.name, candidates.length]))) return;
    await act('clean', () => run('delete_voice_candidates', { speaker: speaker.id }), '清理失败');
  };

  return (
    <div className="flex-grow overflow-y-auto px-5 pb-6 pt-4 space-y-5">
      {/* 1. 固定试听句：所有音色都念它 */}
      <section className="space-y-2">
        <h4 className="text-[11px] font-bold tracking-wider text-gray-400">试听句</h4>
        <p data-i18n-skip className="bg-white border border-[#e5e5e0] rounded-2xl px-4 py-3 text-[14px] leading-relaxed text-gray-800">{previewText}</p>
      </section>

      {/* 2. 取材方式：每种一行，做过的可试听 / 使用，没做的点「克隆」 */}
      <section className="space-y-2">
        <h4 className="text-[11px] font-bold tracking-wider text-gray-400">音色</h4>
        {!currentClone && (
          <div className="rounded-2xl border border-dashed border-claude-accent/40 bg-claude-accent/5 px-4 py-4 space-y-3">
            <p className="text-[12px] text-gray-500 leading-relaxed">
              {speaker.voiceLabel ? `现在用的是系统音色「${speaker.voiceLabel}」。` : ''}用原片里这个人的声音来配音。
            </p>
            <button
              type="button"
              disabled={busy !== null || !hasMaterial}
              onClick={() => void cloneDefault()}
              className="w-full text-[14px] font-bold text-white bg-claude-accent hover:opacity-90 rounded-xl py-3 disabled:opacity-40 transition-opacity"
            >
              {busy === 'gen-default' ? '克隆中，要几十秒…' : '克隆这个人的音色'}
            </button>
            {!hasMaterial && (
              <p className="text-[11px] text-gray-400">这个人没有可用的素材，做不了克隆。</p>
            )}
          </div>
        )}

        <ul className="space-y-2">
          {rows.map((row) => {
            const v = row.v;
            const made = v !== null;
            const off = !made && row.option !== null && !row.option.available;
            const isPlaying = made && player.playing === `v-${row.key}`;
            const loading = made && wantPlay === row.key;
            const cloning = busy === `gen-${row.key}`;
            const sub = made
              ? rangeText(v.lines, v.seconds)
              : off
                ? tBackend(row.option?.reason ?? '') || t('做不了')
                : (rangeText(row.option?.lines ?? [], row.option?.seconds ?? null) || tBackend(row.option?.hint ?? ''));
            return (
              <li
                key={row.key}
                className={[
                  'flex items-center gap-3 rounded-2xl border px-3 py-3 bg-white',
                  row.current ? 'border-claude-accent/60' : 'border-[#e5e5e0]',
                  off ? 'opacity-60' : '',
                ].join(' ')}
              >
                <button
                  type="button"
                  onClick={() => { if (made) void listen(row as MadeRow); }}
                  disabled={!made || (busy !== null && !loading)}
                  className={[
                    'w-10 h-10 shrink-0 rounded-full flex items-center justify-center text-[13px] transition-colors',
                    isPlaying ? 'bg-claude-accent text-white' : 'bg-claude-accent/10 text-claude-accent hover:bg-claude-accent/20',
                    'disabled:opacity-30 disabled:hover:bg-claude-accent/10',
                  ].join(' ')}
                  title={!made ? '先克隆才能试听' : isPlaying ? '停止' : '听这个音色念上面那段'}
                >
                  {loading ? '…' : isPlaying ? '■' : '▶'}
                </button>

                <div className="min-w-0 flex-grow">
                  <div className="flex items-center gap-2">
                    <span className="text-[14px] font-bold text-gray-800 truncate">{row.name}</span>
                    {row.current && <span className="text-[10px] font-bold text-claude-accent">正在使用</span>}
                    {made && !v.usable && <span className="text-[10px] font-bold text-red-500" title="按旧的 TTS 模型克隆的，不能再用">已失效</span>}
                  </div>
                  {sub && <div className="text-[11px] text-gray-400 truncate" title={sub}>{sub}</div>}
                </div>

                {made && !row.current && (
                  <button
                    type="button"
                    disabled={busy !== null || !v.usable}
                    onClick={() => void use(row as MadeRow)}
                    className="shrink-0 text-[12px] font-bold text-white bg-claude-accent hover:opacity-90 rounded-xl px-4 py-2 disabled:opacity-40 transition-opacity"
                  >
                    {busy === `use-${v.id}` ? '…' : '使用'}
                  </button>
                )}
                {!made && !off && (
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() => void clone(row)}
                    className="shrink-0 text-[12px] font-bold text-claude-accent border border-claude-accent/60 hover:bg-claude-accent/5 rounded-xl px-4 py-2 disabled:opacity-40 transition-colors"
                  >
                    {cloning ? '克隆中…' : '克隆'}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {(thin || candidates.length > 0) && (
        <section className="space-y-2 text-center">
          {thin && (
            <p className="text-[11px] text-amber-600 leading-relaxed">
              {t('这个人的素材不多（共')} <span>{material.total}</span> {t('秒），克隆可能不太稳，多试几种再挑。')}
            </p>
          )}
          {candidates.length > 0 && (
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => void clean()}
              className="block mx-auto text-[11px] text-gray-400 hover:text-red-500 underline-offset-2 hover:underline disabled:opacity-50"
            >
              {busy === 'clean' ? '清理中…' : `删掉没在用的 ${candidates.length} 个音色`}
            </button>
          )}
        </section>
      )}
    </div>
  );
};

export default VoicePanel;
