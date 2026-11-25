#!/usr/bin/env python3
"""量化对比两套分离输出（mono，44.1kHz）。用法见 README。
输入目录默认 /tmp/sep-out/，要有 mix.wav mdx_vocals.wav mdx_instrumental.wav roformer_lead.wav roformer_instrumental.wav"""
import sys, numpy as np, subprocess
D = sys.argv[1] if len(sys.argv) > 1 else '/tmp/sep-out/'
def mono(p, ar=44100):
    r = subprocess.run(['ffmpeg','-v','error','-i',p,'-f','f32le','-ac','1','-ar',str(ar),'-'], capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.float32)
db = lambda x: 20*np.log10(np.sqrt(np.mean(x**2))+1e-12)
def band(x, lo, hi, ar=44100):
    X = np.abs(np.fft.rfft(x))**2; f = np.fft.rfftfreq(len(x), 1/ar); return 10*np.log10(X[(f>=lo)&(f<hi)].sum()+1e-15)
mix, mv, mb = mono(D+'mix.wav'), mono(D+'mdx_vocals.wav'), mono(D+'mdx_instrumental.wav')
rv, rb = mono(D+'roformer_lead.wav'), mono(D+'roformer_instrumental.wav')
n = min(len(x) for x in (mix, mv, mb, rv, rb)); mix,mv,mb,rv,rb = [x[:n] for x in (mix,mv,mb,rv,rb)]
print(f"样本 {n/44100:.1f}s | 混音 {db(mix):.1f} dB | 背景轨比人声轨响 = 真有伴奏\n")
print(f"{'指标':22}{'MDX':>9}{'Roformer':>11}   说明")
print(f"{'人声轨 RMS':22}{db(mv):>9.1f}{db(rv):>11.1f}")
print(f"{'背景轨 RMS':22}{db(mb):>9.1f}{db(rb):>11.1f}")
print(f"{'人声 6-10k 相对混音':22}{band(mv,6000,10000)-band(mix,6000,10000):>9.1f}{band(rv,6000,10000)-band(mix,6000,10000):>11.1f}   齿擦音区；0=全保留")
print(f"{'背景 6-10k 相对混音':22}{band(mb,6000,10000)-band(mix,6000,10000):>9.1f}{band(rb,6000,10000)-band(mix,6000,10000):>11.1f}   背景轨应保留高频")
w = int(0.3*44100); e = np.array([np.mean(mix[i:i+w]**2) for i in range(0, n-w, w)])
quiet = np.argsort(e)[:5]*w; loud = np.argsort(e)[-8:]*w
print(f"{'人声轨静音段残留':22}{10*np.log10(np.mean([np.mean(mv[i:i+w]**2) for i in quiet])+1e-15):>9.1f}{10*np.log10(np.mean([np.mean(rv[i:i+w]**2) for i in quiet])+1e-15):>11.1f}   越低越好")
def bandSeg(x, segs, lo=300, hi=3400, ar=44100):
    tot=0;cnt=0
    for s in segs:
        seg=x[s:s+w]; X=np.abs(np.fft.rfft(seg))**2; f=np.fft.rfftfreq(len(seg),1/ar); tot+=X[(f>=lo)&(f<hi)].sum(); cnt+=1
    return 10*np.log10(tot/cnt+1e-15)
print(f"{'说话段背景轨人声残留':22}{bandSeg(mb,loud)-bandSeg(mix,loud):>9.1f}{bandSeg(rb,loud)-bandSeg(mix,loud):>11.1f}   越低越好")
print(f"{'重构误差 mix-(v+b)':22}{db(mix-mv-mb):>9.1f}{db(mix-rv-rb):>11.1f}   两轨相加应还原混音")
