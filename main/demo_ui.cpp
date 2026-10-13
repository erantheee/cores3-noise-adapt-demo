#include "demo_ui.hpp"

#include <cstdio>
#include "lvgl.h"

namespace {
lv_obj_t* env_value;
lv_obj_t* action_value;
lv_obj_t* voice_value;
lv_obj_t* nr_value;
lv_obj_t* media_value;
lv_obj_t* link_value;
lv_obj_t* detail_value;
lv_obj_t* confidence_bar;

lv_obj_t* make_card(lv_obj_t* parent, int x, int y, int w, int h,
                    const char* title, lv_obj_t** value) {
    lv_obj_t* card = lv_obj_create(parent);
    lv_obj_set_pos(card, x, y);
    lv_obj_set_size(card, w, h);
    lv_obj_set_style_radius(card, 10, 0);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x171A20), 0);
    lv_obj_set_style_border_color(card, lv_color_hex(0x2A303A), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_pad_all(card, 7, 0);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t* label = lv_label_create(card);
    lv_label_set_text(label, title);
    lv_obj_set_style_text_color(label, lv_color_hex(0x8E99A8), 0);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_12, 0);
    *value = lv_label_create(card);
    lv_obj_align(*value, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    lv_obj_set_style_text_color(*value, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(*value, &lv_font_montserrat_20, 0);
    return card;
}
}  // namespace

void demo_ui_create() {
    lv_obj_t* screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x0B0D11), 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0xFFFFFF), 0);

    lv_obj_t* title = lv_label_create(screen);
    lv_label_set_text(title, "EDGE AUDIO CONTROLLER");
    lv_obj_set_pos(title, 10, 7);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_14, 0);

    lv_obj_t* badge = lv_label_create(screen);
    lv_label_set_text(badge, "AUTO");
    lv_obj_align(badge, LV_ALIGN_TOP_RIGHT, -10, 6);
    lv_obj_set_style_text_color(badge, lv_color_hex(0x5FF2A7), 0);
    lv_obj_set_style_text_font(badge, &lv_font_montserrat_14, 0);

    make_card(screen, 8, 30, 98, 62, "ENV", &env_value);
    make_card(screen, 111, 30, 201, 62, "ACTION", &action_value);
    make_card(screen, 8, 97, 98, 62, "VOICE GAIN", &voice_value);
    make_card(screen, 111, 97, 98, 62, "NOISE RED.", &nr_value);
    make_card(screen, 214, 97, 98, 62, "MEDIA", &media_value);
    make_card(screen, 8, 164, 98, 47, "EARBUD LINK", &link_value);

    detail_value = lv_label_create(screen);
    lv_obj_set_pos(detail_value, 116, 169);
    lv_obj_set_style_text_color(detail_value, lv_color_hex(0x9BA5B4), 0);
    lv_obj_set_style_text_font(detail_value, &lv_font_montserrat_12, 0);

    confidence_bar = lv_bar_create(screen);
    lv_obj_set_pos(confidence_bar, 116, 197);
    lv_obj_set_size(confidence_bar, 196, 8);
    lv_bar_set_range(confidence_bar, 0, 100);
    lv_obj_set_style_bg_color(confidence_bar, lv_color_hex(0x232833), LV_PART_MAIN);
    lv_obj_set_style_bg_color(confidence_bar, lv_color_hex(0x5FF2A7), LV_PART_INDICATOR);

    lv_obj_t* footer = lv_label_create(screen);
    lv_label_set_text(footer, "Processor demo - not final OWS acoustics");
    lv_obj_align(footer, LV_ALIGN_BOTTOM_MID, 0, -5);
    lv_obj_set_style_text_color(footer, lv_color_hex(0x697384), 0);
    lv_obj_set_style_text_font(footer, &lv_font_montserrat_10, 0);
}

void demo_ui_update(const AnalysisFrame& frame, const EarbudCommand& command,
                    float processing_ms, uint32_t free_heap) {
    char text[96];
    std::snprintf(text, sizeof(text), "%s\n%.0f dBFS", environment_name(command.environment),
                  frame.broadband_dbfs);
    lv_label_set_text(env_value, text);
    lv_label_set_text(action_value, adapt_state_name(command.state));

    std::snprintf(text, sizeof(text), "+%.1f dB", command.speech_gain_db);
    lv_label_set_text(voice_value, text);
    std::snprintf(text, sizeof(text), "-%.1f dB", command.noise_reduction_db);
    lv_label_set_text(nr_value, text);
    std::snprintf(text, sizeof(text), "%u%%", command.media_percent);
    lv_label_set_text(media_value, text);

    lv_label_set_text(link_value, command.earbud_link_connected ? "SENT" : "SIMULATED");
    lv_obj_set_style_text_color(link_value,
        command.earbud_link_connected ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFFC857), 0);

    std::snprintf(text, sizeof(text), "Mic %s  |  DSP %.1fms  |  Heap %luk",
                  frame.mic_valid ? "OK" : "FAULT", processing_ms,
                  static_cast<unsigned long>(free_heap / 1024));
    lv_label_set_text(detail_value, text);
    lv_bar_set_value(confidence_bar, static_cast<int32_t>(command.confidence * 100.0f), LV_ANIM_OFF);
}
