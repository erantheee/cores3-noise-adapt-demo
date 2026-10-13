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

const char* environment_text(EnvironmentKind environment) {
    switch (environment) {
        case EnvironmentKind::quiet: return "安静";
        case EnvironmentKind::steady_noise: return "噪声";
        case EnvironmentKind::voice_likely: return "人声嘈杂";
        case EnvironmentKind::loud: return "高噪声";
    }
    return "未知";
}

const char* action_text(AdaptState state) {
    switch (state) {
        case AdaptState::observing: return "正在感知";
        case AdaptState::adjusting: return "正在调节";
        case AdaptState::holding: return "保持当前设置";
        case AdaptState::recovering: return "正在恢复";
        case AdaptState::mic_fault: return "麦克风异常";
    }
    return "未知";
}

lv_obj_t* make_card(lv_obj_t* parent, int x, int y, int w, int h,
                    const char* title, lv_obj_t** value) {
    lv_obj_t* card = lv_obj_create(parent);
    lv_obj_set_pos(card, x, y);
    lv_obj_set_size(card, w, h);
    lv_obj_set_style_radius(card, 10, 0);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x171A20), 0);
    lv_obj_set_style_border_color(card, lv_color_hex(0x2A303A), 0);
    lv_obj_set_style_border_width(card, 1, 0);
    lv_obj_set_style_pad_all(card, 6, 0);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t* label = lv_label_create(card);
    lv_label_set_text(label, title);
    lv_obj_set_style_text_color(label, lv_color_hex(0x8E99A8), 0);
    lv_obj_set_style_text_font(label, &lv_font_simsun_16_cjk, 0);
    *value = lv_label_create(card);
    lv_obj_set_pos(*value, 0, 25);
    lv_obj_set_width(*value, w - 14);
    lv_label_set_long_mode(*value, LV_LABEL_LONG_CLIP);
    lv_obj_set_style_text_color(*value, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(*value, &lv_font_simsun_16_cjk, 0);
    return card;
}
}  // namespace

void demo_ui_create() {
    lv_obj_t* screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x0B0D11), 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0xFFFFFF), 0);

    lv_obj_t* title = lv_label_create(screen);
    lv_label_set_text(title, "端侧听音控制器");
    lv_obj_set_pos(title, 10, 7);
    lv_obj_set_style_text_font(title, &lv_font_simsun_16_cjk, 0);

    lv_obj_t* badge = lv_label_create(screen);
    lv_label_set_text(badge, "自动");
    lv_obj_set_pos(badge, 270, 7);
    lv_obj_set_style_text_color(badge, lv_color_hex(0x5FF2A7), 0);
    lv_obj_set_style_text_font(badge, &lv_font_simsun_16_cjk, 0);

    make_card(screen, 10, 31, 92, 60, "环境", &env_value);
    make_card(screen, 108, 31, 202, 60, "当前动作", &action_value);
    make_card(screen, 10, 97, 96, 60, "人声增益", &voice_value);
    make_card(screen, 112, 97, 96, 60, "降噪", &nr_value);
    make_card(screen, 214, 97, 96, 60, "媒体音量", &media_value);
    make_card(screen, 10, 163, 96, 49, "耳机连接", &link_value);

    lv_obj_set_style_text_font(voice_value, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_font(nr_value, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_font(media_value, &lv_font_montserrat_20, 0);

    detail_value = lv_label_create(screen);
    lv_obj_set_pos(detail_value, 112, 168);
    lv_obj_set_style_text_color(detail_value, lv_color_hex(0x9BA5B4), 0);
    lv_obj_set_style_text_font(detail_value, &lv_font_simsun_16_cjk, 0);

    confidence_bar = lv_bar_create(screen);
    lv_obj_set_pos(confidence_bar, 112, 200);
    lv_obj_set_size(confidence_bar, 198, 8);
    lv_bar_set_range(confidence_bar, 0, 100);
    lv_obj_set_style_bg_color(confidence_bar, lv_color_hex(0x232833), LV_PART_MAIN);
    lv_obj_set_style_bg_color(confidence_bar, lv_color_hex(0x5FF2A7), LV_PART_INDICATOR);

    lv_obj_t* footer = lv_label_create(screen);
    lv_label_set_text(footer, "算法验证机 · 非最终耳机音效");
    lv_obj_set_pos(footer, 48, 220);
    lv_obj_set_style_text_color(footer, lv_color_hex(0x697384), 0);
    lv_obj_set_style_text_font(footer, &lv_font_simsun_16_cjk, 0);
}

void demo_ui_update(const AnalysisFrame& frame, const EarbudCommand& command,
                    float processing_ms, uint32_t free_heap) {
    char text[96];
    lv_label_set_text(env_value, environment_text(command.environment));
    lv_label_set_text(action_value, action_text(command.state));

    std::snprintf(text, sizeof(text), "+%.1f dB", command.speech_gain_db);
    lv_label_set_text(voice_value, text);
    std::snprintf(text, sizeof(text), "-%.1f dB", command.noise_reduction_db);
    lv_label_set_text(nr_value, text);
    std::snprintf(text, sizeof(text), "%u%%", command.media_percent);
    lv_label_set_text(media_value, text);

    lv_label_set_text(link_value, command.earbud_link_connected ? "已发送" : "模拟");
    lv_obj_set_style_text_color(link_value,
        command.earbud_link_connected ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFFC857), 0);

    std::snprintf(text, sizeof(text), "麦克风%s  处理%.1fms  内存%luk",
                  frame.mic_valid ? "正常" : "异常", processing_ms,
                  static_cast<unsigned long>(free_heap / 1024));
    lv_label_set_text(detail_value, text);
    lv_bar_set_value(confidence_bar, static_cast<int32_t>(command.confidence * 100.0f), LV_ANIM_OFF);
}
