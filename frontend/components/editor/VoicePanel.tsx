import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CloneVoiceView, CommandResult, Line, SpeakerView } from '../../net/types';
import type { HeaderProps } from '../../hooks/useWindows';

/**
 * 音色面板：一次只看一个说话人，听几个克隆音色念同一句固定的话，挑一个用。
 *
 * 界面只有三块：顶部切换说话人 → 那句固定的试听句 → 音色列表（每行 = 播放 + 使用）。
 * 试听句是固定的（不是这个工程里的某句译文）：换了工程、没翻译完都能对比。
 * 做更多音色只是底部一个不显眼的链接，费用只在点击后的确认框里出现；手动指定参考句这类少用的
 * 操作留给聊天里的 AI 助手。试听文件没有时，点播放才合成。
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

/** 每个新建音色的参考价（元）。 */
const PRICE_PER_VOICE = 0.01;

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

interface VoicesProps {
  speaker: SpeakerView;
  lines: Line[];
  player: ReturnType<typeof useSharedPlayer>;
  run: Run;
  onNotice: (m: string) => void;
}

const SpeakerVoices: React.FC<VoicesProps> = ({ speaker, lines, player, run, onNotice }) => {
  const [busy, setBusy] = useState<string | null>(null);
  // 点了播放但试听还没合成：合成好、地址随状态推来之后自动播
  const [wantPlay, setWantPlay] = useState<string | null>(null);

  const { currentClone, candidates, previewText, presetOptions, material } = speaker;
  const rows = useMemo(() => {
    const out: { key: string; v: CloneVoiceView; current: boolean; name: string }[] = [];
    if (currentClone) out.push({ key: 'cur', v: currentClone, current: true, name: currentClone.presetLabel ?? '当前音色' });
    candidates.forEach((c, i) => out.push({ key: c.id ?? `c${i}`, v: c, current: false, name: c.presetLabel ?? '原来的音色' }));
    return out;
  }, [currentClone, candidates]);

  const translated = useMemo(
    () => lines.filter((l) => l.speakerId === speaker.id && l.kind === 'speech' && l.translation.trim()),
    [lines, speaker.id],
  );

  // 还能再做几个：没做过的、并且这个说话人做得了的规则型预设
  const have = useMemo(() => new Set(rows.map((r) => r.v.preset).filter(Boolean) as string[]), [rows]);
  const makeable = presetOptions.filter((o) => o.id !== 'smart' && o.available && !have.has(o.id));
  const thin = material.total < 10 || material.longest < 5;

  useEffect(() => {
    if (!wantPlay) return;
    const row = rows.find((r) => r.key === wantPlay);
    if (row?.v.previewUrl) { player.play(`v-${wantPlay}`, row.v.previewUrl); setWantPlay(null); }
  }, [wantPlay, rows, player]);

  const act = useCallback(async (key: string, fn: () => Promise<CommandResult>, fail: string) => {
    setBusy(key);
    try {
      const r = await fn();
      if (!r.ok) onNotice(r.error ?? fail);
      return r;
    } finally {
      setBusy(null);
    }
  }, [onNotice]);

  const listen = async (row: { key: string; v: CloneVoiceView }) => {
    if (player.playing === `v-${row.key}`) { player.stop(); return; }
    if (row.v.previewUrl) { player.play(`v-${row.key}`, row.v.previewUrl); return; }
    setWantPlay(row.key);
    const r = await act('prev', () => run('preview_voice_candidates', { speaker: speaker.id }), '合成试听失败');
    if (!r.ok) setWantPlay(null);
  };

  const generate = async () => {
    const n = makeable.length;
    const cost = (n * PRICE_PER_VOICE).toFixed(2);
    if (!window.confirm(`为 ${speaker.name} 新建 ${n} 个音色？\n约 ${cost} 元，每个要几十秒，做好后可以直接试听挑选。`)) return;
    await act('gen', () => run('generate_voice_candidates', { speaker: speaker.id, presets: makeable.map((o) => o.id) }), '生成失败');
  };

  const use = async (row: { v: CloneVoiceView; name: string }) => {
    if (!row.v.id) return;
    const r = await act(`use-${row.v.id}`, () => run('use_voice_candidate', { speaker: speaker.id, candidate: row.v.id }), '换用失败');
    if (!r.ok) return;
    const todo = translated.filter((l) => !l.muted).map((l) => l.line);
    if (!todo.length) return;
    // 换音色不花钱，重新配音才花钱：先问清楚。取消 = 保持现有配音，那些句子标成「需更新」。
    const go = window.confirm(
      `已换成「${row.name}」。\n${speaker.name} 的 ${todo.length} 句配音要用新音色重新合成，会产生 TTS 费用。\n\n现在重新配音吗？\n（选“取消”则先保持现有配音，这些句子会标为「需更新」，之后点顶栏的「补」。）`,
    );
    if (go) await act('redub', () => run('redub', { lines: todo }), '重新配音失败');
  };

  const clean = async () => {
    if (!window.confirm(`删掉 ${speaker.name} 没在用的 ${candidates.length} 个音色？\n服务端的也会一起删除，无法找回。`)) return;
    await act('clean', () => run('delete_voice_candidates', { speaker: speaker.id }), '清理失败');
  };

  const working = busy === 'gen';

  return (
    <div className="flex-grow overflow-y-auto px-5 pb-6 pt-4 space-y-5">
      {/* 1. 固定试听句：所有音色都念它 */}
      <section className="space-y-2">
        <h4 className="text-[11px] font-bold tracking-wider text-gray-400">试听句</h4>
        <p className="bg-white border border-[#e5e5e0] rounded-2xl px-4 py-3 text-[14px] leading-relaxed text-gray-800">{previewText}</p>
      </section>

      {/* 2. 音色列表 */}
      <section className="space-y-2">
        <h4 className="text-[11px] font-bold tracking-wider text-gray-400">
          {rows.length ? '音色' : '还没有克隆音色'}
        </h4>

        {rows.length === 0 && (
          <p className="text-[13px] text-gray-500 leading-relaxed">
            {speaker.voiceLabel ? `现在用的是系统音色「${speaker.voiceLabel}」。` : ''}克隆会用原片里这个人的声音来配音。
          </p>
        )}

        <ul className="space-y-2">
          {rows.map((row) => {
            const isPlaying = player.playing === `v-${row.key}`;
            const loading = wantPlay === row.key;
            return (
              <li
                key={row.key}
                className={[
                  'flex items-center gap-3 rounded-2xl border px-3 py-3 bg-white',
                  row.current ? 'border-claude-accent/60' : 'border-[#e5e5e0]',
                ].join(' ')}
              >
                <button
                  type="button"
                  onClick={() => void listen(row)}
                  disabled={busy !== null && !loading}
                  className={[
                    'w-10 h-10 shrink-0 rounded-full flex items-center justify-center text-[13px] transition-colors disabled:opacity-40',
                    isPlaying ? 'bg-claude-accent text-white' : 'bg-claude-accent/10 text-claude-accent hover:bg-claude-accent/20',
                  ].join(' ')}
                  title={isPlaying ? '停止' : '听这个音色念上面那句'}
                >
                  {loading ? '…' : isPlaying ? '■' : '▶'}
                </button>

                <div className="min-w-0 flex-grow">
                  <div className="flex items-center gap-2">
                    <span className="text-[14px] font-bold text-gray-800 truncate">{row.name}</span>
                    {row.current && <span className="text-[10px] font-bold text-claude-accent">正在使用</span>}
                    {!row.v.usable && <span className="text-[10px] font-bold text-red-500" title="按旧的 TTS 模型克隆的，不能再用">已失效</span>}
                  </div>
                  {row.v.lines.length > 0 && (
                    <div className="text-[11px] text-gray-400 truncate">
                      取自第 {row.v.lines.join('、')} 句{row.v.seconds ? ` · ${row.v.seconds} 秒` : ''}
                    </div>
                  )}
                </div>

                {!row.current && (
                  <button
                    type="button"
                    disabled={busy !== null || !row.v.usable}
                    onClick={() => void use(row)}
                    className="shrink-0 text-[12px] font-bold text-white bg-claude-accent hover:opacity-90 rounded-xl px-4 py-2 disabled:opacity-40 transition-opacity"
                  >
                    {busy === `use-${row.v.id}` ? '…' : '使用'}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {/* 3. 做更多：不显眼的链接，费用只在点击后的确认框里写 */}
      <section className="space-y-2 text-center">
        {makeable.length > 0 ? (
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void generate()}
            className="text-[12px] text-gray-500 hover:text-claude-accent underline-offset-2 hover:underline disabled:opacity-50 transition-colors"
          >
            {working ? '正在做，每个要几十秒…' : rows.length ? '试试其他取材方式' : '克隆这个人的音色'}
          </button>
        ) : rows.length === 0 ? (
          <p className="text-[12px] text-gray-400">
            {presetOptions.find((o) => o.id !== 'smart' && !o.available)?.reason ?? '没有可用的素材，做不了克隆。'}
          </p>
        ) : null}

        {thin && rows.length > 0 && (
          <p className="text-[11px] text-amber-600 leading-relaxed">
            这个人的素材不多（共 {material.total} 秒），克隆可能不太稳，多试几种再挑。
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
    </div>
  );
};

export default VoicePanel;
