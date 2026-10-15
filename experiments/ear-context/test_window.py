"""Check firmware feature ordering against the existing Python training pipeline."""
import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import numpy as np

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root.parent / 'listening-model/scripts'))
from features import window_vector

source = r'''
#include <cstdio>
#include "window.hpp"
int main() {
    Window w;
    for(auto& r:w) {
        if(scanf("%lf", &r.t_s)!=1) return 2;
        for(auto& x:r.frame.band_dbfs) scanf("%f",&x);
        double excited;
        scanf("%f%f%f%f%f%lf", &r.frame.broadband_dbfs,&r.frame.voice_likelihood,
          &r.vf.spectral_flatness,&r.vf.vocal_peakiness,&r.vf.vocal_ratio,&excited);
        r.vf.excited_vocal_bands=static_cast<size_t>(excited);
    }
    float f[92]; build_window(w,f);
    for(float x:f) printf("%.9g\n",x);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp)/'check.cpp'
    src.write_text(source)
    exe = Path(tmp)/'check'
    subprocess.run(['c++','-std=c++17','-O2','-I'+str(root/'main'),
        '-I'+str(root.parent.parent/'main'),str(src),'-o',str(exe)],check=True)
    rng = np.random.default_rng(20261015)
    for trial in range(50):
        frames = {'t_s': np.cumsum(rng.uniform(.08,.12,20)) + trial * 10000,
            'band_dbfs': rng.uniform(-100,-10,(20,24)).astype(np.float32),
            'broadband_dbfs': rng.uniform(-60,-20,20).astype(np.float32),
            'voice_likelihood': rng.uniform(0,1,20).astype(np.float32),
            'spectral_flatness': rng.uniform(0,1,20).astype(np.float32),
            'vocal_peakiness': rng.uniform(0,1,20).astype(np.float32),
            'vocal_ratio': rng.uniform(0,1,20).astype(np.float32),
            'excited_vocal_bands': rng.integers(0,12,20)}
        rows = np.column_stack(list(frames.values()))
        output = subprocess.check_output([str(exe)],input='\n'.join(
            ' '.join(str(x) for x in row) for row in rows).encode())
        actual = np.fromstring(output.decode(),sep='\n')
        expected = window_vector(frames,0,20,when=datetime.datetime(2024,1,1))
        np.testing.assert_allclose(actual,expected,atol=2e-5,rtol=2e-5)
print('50 windows / 4600 feature values match the training pipeline')
