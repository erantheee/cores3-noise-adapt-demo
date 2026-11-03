#pragma once
#include "sound_sources.hpp"

// No overlapping, human-labelled S3 training set has been accepted yet.
// export_model.py replaces this accessor only after an explicit artifact export.
namespace sound_sources_model {
inline const sound_sources::Model* get() { return nullptr; }
}
