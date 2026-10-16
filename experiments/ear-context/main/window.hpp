#pragma once
#include <array>
#include <cmath>
#include <algorithm>
#include "controller.hpp"
struct FrameRecord { AnalysisFrame frame; VoiceFeatures vf; double t_s; };
using Window = std::array<FrameRecord, 20>;
// Same feature order as listening-model/scripts/features.py. No heap allocations.
inline void build_window(const Window& w, float* out) {
    auto stats = [](const std::array<double,20>& v) {
        std::array<double,4> s{0,0,v[0],v[0]};
        for (double x:v) { s[0]+=x/20; s[2]=std::min(s[2],x); s[3]=std::max(s[3],x); }
        for (double x:v) s[1]+=(x-s[0])*(x-s[0])/20;
        s[1]=std::sqrt(s[1]); return s;
    };
    double tb=0; for (const auto& r:w) tb+=r.t_s/20;
    for (int b=0;b<24;++b) {
        std::array<double,20> v{};
        for(int i=0;i<20;++i) v[i]=w[i].frame.band_dbfs[b];
        auto s=stats(v); out[b]=s[0]; out[24+b]=s[1];
        double cov=0,var=0;
        for(int i=0;i<20;++i) { double dt=w[i].t_s-tb; cov+=dt*(v[i]-s[0]); var+=dt*dt; }
        out[48+b]=var>0 ? cov/var : 0;
    }
    std::array<double,20> level{},voice{};
    int active=0, transitions=0,steps=0;
    double flat=0,peak=0,ratio=0,excited=0;
    for(int i=0;i<20;++i) {
        level[i]=w[i].frame.broadband_dbfs;
        // Ungated per-frame product, NOT frame.voice_likelihood.
        //
        // frame.voice_likelihood has had NoiseAnalyzer's temporal modulation_term() multiplied
        // into it since 2026-10-16 (plan-status items 31/32). The model was trained before that
        // gate existed: features.py::voice_likelihood() is exactly this four-judge product. Two
        // window dimensions (vl_mean/vl_std) read this value, so feeding the gated one is an
        // out-of-distribution input -- silently, as always. vf is already stored per frame, so
        // the ungated product is one call away.
        voice[i]=voice_likelihood_from_features(w[i].vf);
        active+=voice[i]>0.55;
        if(i) { transitions+=((voice[i]>0.55)!=(voice[i-1]>0.55)); steps+=std::abs(level[i]-level[i-1])>3; }
        flat+=w[i].vf.spectral_flatness; peak+=w[i].vf.vocal_peakiness;
        ratio+=w[i].vf.vocal_ratio; excited+=w[i].vf.excited_vocal_bands;
    }
    auto ls=stats(level), vs=stats(voice);
    for(int i=0;i<4;++i) out[72+i]=ls[i];
    out[76]=vs[0]; out[77]=vs[1]; out[78]=active/20.f; out[79]=transitions;
    out[80]=flat/20; out[81]=peak/20; out[82]=ratio/20; out[83]=excited/20; out[84]=steps;
    // v0.2 was trained with motion=0. Raw IMU must not enter these inputs yet.
    out[85]=out[86]=0;
    // Missing-clock baseline, and it has to be the SAME one the training pipeline uses.
    //
    // features.py::window_vector(when=None) defaults to hour_frac=0.5, dow=2 -> (0, -1,
    // 0.974928, -0.222521, 0). This file used to hardcode Monday 00:00, i.e. (0, +1, 0, +1, 0),
    // which is 4.04 sigma away in normalized space on hour_cos and flipped 3 of the 27
    // model-bench self-check vectors (measured 2026-10-16). The model was trained on randomly
    // assigned times (make_dataset.py::random_when), so these five dimensions carry a
    // distribution -- they are not constants, and any constant that is not the documented one
    // is an out-of-distribution input that reports no error.
    //
    // main/context_window.hpp is the authoritative version of this; keep the two identical.
    out[87]=0.f; out[88]=-1.f; out[89]=0.974928f; out[90]=-0.222521f; out[91]=0.f;
}
