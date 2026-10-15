#include "ui.hpp"
#include "lvgl.h"
#include <cstdio>
#include <algorithm>
LV_FONT_DECLARE(ear_font16);
LV_FONT_DECLARE(ear_font24);
static lv_obj_t *home,*detail,*scene,*subtitle,*level,*motion,*meter,*diagnostic,*nav;
static bool details=false;
static lv_obj_t* label(lv_obj_t* p,int x,int y,int width,const char* text,uint32_t color=0xDCE8F2) {
    auto l=lv_label_create(p); lv_obj_set_pos(l,x,y); lv_obj_set_width(l,width);
    lv_obj_set_style_text_color(l,lv_color_hex(color),0); lv_label_set_text(l,text); return l;
}
static lv_obj_t* panel(lv_obj_t* p,int x,int y,int w,int h) {
    auto o=lv_obj_create(p); lv_obj_set_pos(o,x,y); lv_obj_set_size(o,w,h);
    lv_obj_remove_flag(o,LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(o,lv_color_hex(0x152536),0);
    lv_obj_set_style_border_width(o,0,0); lv_obj_set_style_radius(o,12,0); lv_obj_set_style_pad_all(o,0,0); return o;
}
void ui_create() {
    auto root=lv_screen_active();
    lv_obj_set_style_bg_color(root,lv_color_hex(0x091520),0);
    lv_obj_set_style_text_font(root,&ear_font16,0);
    lv_obj_remove_flag(root,LV_OBJ_FLAG_SCROLLABLE);
    label(root,12,8,220,"耳戴 · 环境感知",0x64DDD0);
    label(root,257,8,60,"实验版",0x90A7BA);
    home=lv_obj_create(root); detail=lv_obj_create(root);
    for(auto p:{home,detail}) {
        lv_obj_set_pos(p,0,34); lv_obj_set_size(p,320,170);
        lv_obj_set_style_bg_opa(p,LV_OPA_TRANSP,0); lv_obj_set_style_border_width(p,0,0);
        lv_obj_set_style_pad_all(p,0,0); lv_obj_remove_flag(p,LV_OBJ_FLAG_SCROLLABLE);
    }
    auto hero=panel(home,10,0,300,86);
    label(hero,12,6,270,"当前环境 · 模型判断",0x90A7BA);
    scene=label(hero,12,28,270,"正在感知"); lv_obj_set_style_text_font(scene,&ear_font24,0);
    subtitle=label(hero,12,61,276,"先收集约两秒声音",0x90A7BA);
    auto left=panel(home,10,94,145,70);
    label(left,10,6,125,"声音电平",0x90A7BA);
    level=label(left,10,27,125,"-- dBFS");
    meter=lv_bar_create(left); lv_obj_set_pos(meter,10,53); lv_obj_set_size(meter,125,5);
    lv_bar_set_range(meter,0,100); lv_obj_set_style_bg_color(meter,lv_color_hex(0x64DDD0),LV_PART_INDICATOR);
    auto right=panel(home,163,94,147,70);
    label(right,10,6,127,"头部传感器",0x90A7BA);
    motion=label(right,10,27,127,"正在启动");
    label(right,10,49,127,"活动识别待训练",0x90A7BA);
    diagnostic=label(detail,16,3,292,"等待数据");
    lv_obj_set_style_text_line_space(diagnostic,6,0);
    lv_obj_add_flag(detail,LV_OBJ_FLAG_HIDDEN);
    label(root,12,212,232,"仅感知 · 耳机未连接",0x90A7BA);
    auto button=lv_button_create(root); lv_obj_set_pos(button,246,206); lv_obj_set_size(button,64,30);
    lv_obj_set_style_bg_color(button,lv_color_hex(0x254052),0);
    nav=lv_label_create(button); lv_label_set_text(nav,"详情"); lv_obj_center(nav);
    lv_obj_add_event_cb(button,[](lv_event_t*) {
        details=!details;
        if(details) {lv_obj_add_flag(home,LV_OBJ_FLAG_HIDDEN);lv_obj_remove_flag(detail,LV_OBJ_FLAG_HIDDEN);}
        else {lv_obj_add_flag(detail,LV_OBJ_FLAG_HIDDEN);lv_obj_remove_flag(home,LV_OBJ_FLAG_HIDDEN);}
        lv_label_set_text(nav,details?"返回":"详情");
    },LV_EVENT_CLICKED,nullptr);
}
void ui_update(int cls,float score,float db,bool mic,bool imu,float gyro,int64_t us,uint32_t drops) {
    static const char* names[]={"嘈杂人声","鸣笛声","音乐","近处人声","其他声音","安静","持续噪声","街道声","风声"};
    lv_label_set_text(scene,!mic?"麦克风异常":cls<0?"正在感知":score<.65f?"暂不确定":names[cls]);
    char s[384];
    if(cls<0) snprintf(s,sizeof(s),"等待完整声音窗口");
    else snprintf(s,sizeof(s),"模型分数 %.0f%% · 真实效果待验证",score*100);
    lv_label_set_text(subtitle,s);
    snprintf(s,sizeof(s),"%.1f dBFS",db); lv_label_set_text(level,s);
    lv_bar_set_value(meter,mic?std::clamp(int(db+100),0,100):0,LV_ANIM_OFF);
    lv_label_set_text(motion,imu?"六轴采集中":"未收到数据");
    lv_obj_set_style_text_color(motion,lv_color_hex(imu?0x64DDD0:0xFFB86B),0);
    snprintf(s,sizeof(s),"声音候选：%s\n头部转动：%.1f 度/秒\n模型计算：%.2f 毫秒\n输出丢弃：%lu 条\n时钟未同步 · 活动模型待训练\nUSB 数据输出 · 未确认保存",cls>=0?names[cls]:"未知",gyro,us/1000.0,(unsigned long)drops);
    lv_label_set_text(diagnostic,s);
}
