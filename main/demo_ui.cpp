#include "demo_ui.hpp"

#include <cstdio>
#include "esp_log.h"
#include "lvgl.h"

extern "C" {
extern const lv_font_t lv_font_ui_16;
}

namespace {
const char* kTag = "demo_ui";

/// Boot self-check for a generated font.
///
/// The fonts are built with `--no-compress --no-prefilter`, so their glyphs are stored as
/// plain bitmaps (`bitmap_format == LV_FONT_FMT_TXT_PLAIN`). Regenerating one without
/// `--no-compress` stores RLE bitmaps instead, and because `LV_USE_FONT_COMPRESSED` is off
/// in this project `lv_font_get_bitmap_fmt_txt()` then returns NULL for *every* glyph:
/// panels and bars still draw, no text appears anywhere, and nothing is reported
/// (`LV_USE_LOG=0`). This has already happened once on this project with the ear-context
/// font, which is why the check exists. Ported from `experiments/ear-context/main/ui.cpp`.
void check_font(const lv_font_t* font, const char* name) {
    const auto* dsc = static_cast<const lv_font_fmt_txt_dsc_t*>(font->dsc);
    if (!LV_USE_FONT_COMPRESSED && dsc != nullptr &&
        dsc->bitmap_format != LV_FONT_FMT_TXT_PLAIN) {
        ESP_LOGE(kTag,
                 "%s has compressed glyph bitmaps but LV_USE_FONT_COMPRESSED=0: no text will "
                 "be drawn. Regenerate it with --no-compress --no-prefilter.",
                 name);
    }
}

lv_obj_t* env_value;
lv_obj_t* action_value;
lv_obj_t* voice_value;
lv_obj_t* tune_value;
lv_obj_t* media_value;
lv_obj_t* link_value;
/// 「感知」卡片的标题行。分数写在这一行，因为值行被最长的类别名（"near_voice"）占满。
lv_obj_t* context_title;
lv_obj_t* context_value;
lv_obj_t* detail_value;
lv_obj_t* performance_value;
lv_obj_t* confidence_bar;
lv_obj_t* page_main;
lv_obj_t* page_route;
lv_obj_t* route_name_value;
lv_obj_t* route_status_value;
lv_obj_t* mac_status_value;
lv_obj_t* route_hint_value;

const char* environment_text(EnvironmentKind environment) {
    switch (environment) {
        case EnvironmentKind::quiet: return "安静";
        case EnvironmentKind::steady_noise: return "噪声";
        case EnvironmentKind::voice_likely: return "人声嘈杂";
        case EnvironmentKind::loud: return "高噪声";
    }
    return "未知";
}

const char* action_text(const EarbudCommand& command) {
    if (command.state == AdaptState::mic_fault) return "麦克风异常";
    switch (command.tuning_mode) {
        case TuningMode::flat: return "保持当前设置";
        case TuningMode::song_eq: return "正在调音";
        case TuningMode::external_voice_intent: return "人声提升";
        case TuningMode::safety_hold: return "保持当前设置";
    }
    return "未知";
}

const char* conversation_text(ConversationState state) {
    switch (state) {
        case ConversationState::none: return "NONE";
        case ConversationState::speech_nearby: return "SPEECH";
        case ConversationState::likely: return "LIKELY";
        // 预测到对话开始、还没确认。用 ASCII 短串：字库是 115 字子集，别赌某个汉字存在。
        case ConversationState::anticipating: return "PRE";
    }
    return "UNKNOWN";
}

void show_page(bool route_page) {
    if (route_page) {
        lv_obj_add_flag(page_main, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(page_route, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_clear_flag(page_main, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(page_route, LV_OBJ_FLAG_HIDDEN);
    }
}

void gesture_cb(lv_event_t* event) {
    lv_indev_t* input = lv_indev_active();
    if (input == nullptr) return;
    const lv_dir_t direction = lv_indev_get_gesture_dir(input);
    if (direction == LV_DIR_LEFT) show_page(true);
    if (direction == LV_DIR_RIGHT) show_page(false);
    lv_event_stop_bubbling(event);
}

/// `title_out`, when given, receives the title label. The 感知 card needs it because its
/// score has to be written there: the value line is already filled by the longest class name
/// (`near_voice`), and a 60 px-tall card only has room for one value line.
lv_obj_t* make_card(lv_obj_t* parent, int x, int y, int w, int h,
                    const char* title, lv_obj_t** value, lv_obj_t** title_out = nullptr) {
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
    lv_obj_set_style_text_font(label, &lv_font_ui_16, 0);
    if (title_out != nullptr) *title_out = label;
    *value = lv_label_create(card);
    lv_obj_set_pos(*value, 0, 25);
    lv_obj_set_width(*value, w - 14);
    lv_label_set_long_mode(*value, LV_LABEL_LONG_CLIP);
    lv_obj_set_style_text_color(*value, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_style_text_font(*value, &lv_font_ui_16, 0);
    return card;
}
}  // namespace

void demo_ui_create() {
    check_font(&lv_font_ui_16, "lv_font_ui_16");
    lv_obj_t* screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x0B0D11), 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0xFFFFFF), 0);

    lv_obj_add_event_cb(screen, gesture_cb, LV_EVENT_GESTURE, nullptr);

    page_main = lv_obj_create(screen);
    lv_obj_set_pos(page_main, 0, 0);
    lv_obj_set_size(page_main, 320, 240);
    lv_obj_set_style_bg_opa(page_main, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(page_main, 0, 0);
    lv_obj_set_style_pad_all(page_main, 0, 0);
    lv_obj_clear_flag(page_main, LV_OBJ_FLAG_SCROLLABLE);

    page_route = lv_obj_create(screen);
    lv_obj_set_pos(page_route, 0, 0);
    lv_obj_set_size(page_route, 320, 240);
    lv_obj_set_style_bg_opa(page_route, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(page_route, 0, 0);
    lv_obj_set_style_pad_all(page_route, 0, 0);
    lv_obj_clear_flag(page_route, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(page_route, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t* title = lv_label_create(page_main);
    lv_label_set_text(title, "端侧算法验证机");
    lv_obj_set_pos(title, 10, 7);
    lv_obj_set_style_text_font(title, &lv_font_ui_16, 0);

    lv_obj_t* badge = lv_label_create(page_main);
    lv_label_set_text(badge, "自动");
    lv_obj_set_pos(badge, 270, 7);
    lv_obj_set_style_text_color(badge, lv_color_hex(0x5FF2A7), 0);
    lv_obj_set_style_text_font(badge, &lv_font_ui_16, 0);

    // Row 1 is three cards now. 环境 and 当前动作 gave up exactly the width their longest
    // strings do not need ("人声嘈杂" is 4 glyphs = 64 px of the 66 px box; "保持当前设置" is 6
    // = 96 px of 98), and 感知 takes the rest. Nothing moved rows and no card was removed:
    // this is the "add a cell" the integration plan asks for, not a redesign.
    //
    // Why「感知」and not「情境」: lv_font_ui_16.c is a fixed 115-glyph subset and 情 is not in
    // it. The first draft of this card used 情境 and would have drawn "境 0.87" with a hole
    // where 情 belongs -- silently, with no log line, exactly like the compressed-font defect
    // recorded in the README. tools/render_demo_ui.py caught it before flashing. Check any new
    // label against that font (or run that tool) instead of assuming a common character exists.
    make_card(page_main, 10, 31, 80, 60, "环境", &env_value);
    make_card(page_main, 96, 31, 112, 60, "当前动作", &action_value);
    make_card(page_main, 214, 31, 96, 60, "感知", &context_value, &context_title);
    make_card(page_main, 10, 97, 96, 60, "人声状态", &voice_value);
    make_card(page_main, 112, 97, 96, 60, "调音", &tune_value);
    make_card(page_main, 214, 97, 96, 60, "人声增益", &media_value);
    make_card(page_main, 10, 163, 96, 52, "控制链路", &link_value);

    lv_obj_set_style_text_font(voice_value, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_font(tune_value, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_font(media_value, &lv_font_montserrat_20, 0);
    // The class names are the model's own nine labels ("near_voice", "babble", …). They are
    // ASCII only on purpose: this is a status string that must never be blank, and
    // montserrat_14 has no CJK at all, so a class name cannot hit a missing glyph.
    // 96 px of card - 14 px of padding = 82 px; "near_voice" measures 78 px at this size.
    // tools/render_demo_ui.py prints that width for every card, worst case included — run it
    // after touching any of these strings.
    lv_obj_set_style_text_font(context_value, &lv_font_montserrat_14, 0);

    detail_value = lv_label_create(page_main);
    lv_obj_set_pos(detail_value, 112, 158);
    lv_obj_set_style_text_color(detail_value, lv_color_hex(0x5FF2A7), 0);
    lv_obj_set_style_text_font(detail_value, &lv_font_ui_16, 0);

    performance_value = lv_label_create(page_main);
    lv_obj_set_pos(performance_value, 112, 179);
    lv_obj_set_style_text_color(performance_value, lv_color_hex(0x9BA5B4), 0);
    lv_obj_set_style_text_font(performance_value, &lv_font_ui_16, 0);

    confidence_bar = lv_bar_create(page_main);
    lv_obj_set_pos(confidence_bar, 112, 200);
    lv_obj_set_size(confidence_bar, 198, 8);
    lv_bar_set_range(confidence_bar, 0, 100);
    lv_obj_set_style_bg_color(confidence_bar, lv_color_hex(0x232833), LV_PART_MAIN);
    lv_obj_set_style_bg_color(confidence_bar, lv_color_hex(0x5FF2A7), LV_PART_INDICATOR);

    lv_obj_t* footer = lv_label_create(page_main);
    lv_label_set_text(footer, "> 2/2");
    lv_obj_align(footer, LV_ALIGN_BOTTOM_MID, 0, -4);
    lv_obj_set_style_text_color(footer, lv_color_hex(0x697384), 0);
    lv_obj_set_style_text_font(footer, &lv_font_ui_16, 0);

    lv_obj_t* route_title = lv_label_create(page_route);
    lv_label_set_text(route_title, "蓝牙连接");
    lv_obj_set_pos(route_title, 10, 7);
    lv_obj_set_style_text_font(route_title, &lv_font_ui_16, 0);

    make_card(page_route, 10, 34, 300, 58, "耳机", &route_name_value);
    make_card(page_route, 10, 99, 145, 58, "连接状态", &route_status_value);
    make_card(page_route, 165, 99, 145, 58, "控制通道", &mac_status_value);
    route_hint_value = lv_label_create(page_route);
    lv_label_set_text(route_hint_value, "耳机连接 Mac");
    lv_obj_set_pos(route_hint_value, 10, 174);
    lv_obj_set_style_text_color(route_hint_value, lv_color_hex(0x9BA5B4), 0);
    lv_obj_set_style_text_font(route_hint_value, &lv_font_ui_16, 0);

    lv_obj_t* route_footer = lv_label_create(page_route);
    lv_label_set_text(route_footer, "< 1/2");
    lv_obj_align(route_footer, LV_ALIGN_BOTTOM_MID, 0, -4);
    lv_obj_set_style_text_color(route_footer, lv_color_hex(0x697384), 0);
    lv_obj_set_style_text_font(route_footer, &lv_font_ui_16, 0);
}

void demo_ui_update(const AnalysisFrame& frame, const EarbudCommand& command,
                    const MacAudioRoute& route, float processing_ms, uint32_t free_heap) {
    char text[96];
    lv_label_set_text(env_value, environment_text(command.environment));
    lv_label_set_text(action_value, action_text(command));

    // 感知 = 端侧小模型（v0.2-s3，92 维窗）的判断。它与上面「环境」那条**不是一回事**：
    // 环境是本机自己的四分类，感知是模型的九分类，并排显示正是为了看清两者的分歧。
    // 这一轮模型只显示、不参与决策（见 integration-plan.md 第二轮）。
    if (command.context.valid) {
        lv_label_set_text(context_value, command.context.class_name);
        // 分数写在标题行：值行已经被最长的类别名（near_voice）占满。
        std::snprintf(text, sizeof(text), "感知 %.2f",
                      static_cast<double>(command.context.score));
        lv_label_set_text(context_title, text);
        // 低分不做装饰：界面不应该把 0.31 的猜测显示得和 0.95 一样像结论。
        lv_obj_set_style_text_color(
            context_value,
            command.context.score >= 0.65f ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFFFFFF), 0);
    } else {
        // 窗口还没攒够 2 秒，或麦克风输入刚被打断。
        lv_label_set_text(context_value, "--");
        lv_label_set_text(context_title, "感知");
        lv_obj_set_style_text_color(context_value, lv_color_hex(0x697384), 0);
    }

    lv_label_set_text(voice_value, conversation_text(command.conversation));
    // Tuning, not noise reduction: this is how much the speech band is lifted.
    //
    // The text is deliberately digits and Latin only. An earlier draft wrote "9段 +4.2",
    // and 段 is NOT in lv_font_ui_16's 115-glyph subset -- it would have rendered as a
    // blank box on the device, which is exactly the failure the font note in the README
    // warns about. Carrying no CJK here also lets this card keep the crisp 20px numeric font.
    float lifted_db = 0.0f;
    for (std::size_t band = 0; band < kBandCount; ++band) {
        if (command.eq_gain_db[band] > lifted_db) lifted_db = command.eq_gain_db[band];
    }
    std::snprintf(text, sizeof(text), "+%.1f dB", static_cast<double>(lifted_db));
    lv_label_set_text(tune_value, text);
    std::snprintf(text, sizeof(text), "+%.1f dB",
                  static_cast<double>(command.external_voice_gain_db));
    lv_label_set_text(media_value, text);

    lv_label_set_text(link_value, route.mac_connected ? "已连接" : "未连接");
    lv_obj_set_style_text_color(link_value,
        route.mac_connected ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFFC857), 0);

    lv_label_set_text(detail_value, frame.mic_valid ? "麦克风正常" : "麦克风异常");
    lv_obj_set_style_text_color(detail_value,
        frame.mic_valid ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFF6B6B), 0);
    std::snprintf(text, sizeof(text), "处理 %.1fms  内存 %luk", processing_ms,
                  static_cast<unsigned long>(free_heap / 1024));
    lv_label_set_text(performance_value, text);
    lv_bar_set_value(confidence_bar, static_cast<int32_t>(command.confidence * 100.0f), LV_ANIM_OFF);

    lv_label_set_text(route_name_value, route.device_name);
    lv_label_set_text(route_status_value, route.audio_output_active ? "已连接" : "未连接");
    lv_label_set_text(mac_status_value, route.mac_connected ? "已连接" : "未连接");
    lv_obj_set_style_text_color(route_status_value,
        route.audio_output_active ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFFC857), 0);
    lv_obj_set_style_text_color(mac_status_value,
        route.mac_connected ? lv_color_hex(0x5FF2A7) : lv_color_hex(0xFFC857), 0);
}
