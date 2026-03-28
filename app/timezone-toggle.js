// 时区切换模块
(function() {
  'use strict';

  const TIMEZONE_PREF_KEY = 'dpr_timezone_preference'; // 'beijing' | 'utc'

  // 获取时区偏好
  function getTimezonePref() {
    try {
      return localStorage.getItem(TIMEZONE_PREF_KEY) || 'beijing'; // 默认北京时间
    } catch {
      return 'beijing';
    }
  }

  // 设置时区偏好
  function setTimezonePref(pref) {
    try {
      localStorage.setItem(TIMEZONE_PREF_KEY, pref);
      location.reload(); // 刷新页面应用新设置
    } catch {
      // ignore
    }
  }

  // 应用时区显示
  function applyTimezoneDisplay() {
    const pref = getTimezonePref();
    console.log('[Timezone] Applying timezone preference:', pref);

    // 处理运行时间
    document.querySelectorAll('.dpr-runtime').forEach(el => {
      const beijing = el.getAttribute('data-beijing');
      const utc = el.getAttribute('data-utc');

      if (!beijing || !utc) return;

      if (pref === 'beijing') {
        // 北京时间优先
        el.textContent = `${beijing} / ${utc}`;
      } else {
        // UTC 时间优先
        el.textContent = `${utc} / ${beijing}`;
      }
    });

    // 处理侧边栏日期
    document.querySelectorAll('.dpr-sidebar-date').forEach(el => {
      const beijingDate = el.getAttribute('data-beijing-date');
      const utcDate = el.getAttribute('data-utc-date');

      if (!beijingDate || !utcDate) return;

      if (pref === 'beijing') {
        el.textContent = beijingDate;  // 只显示日期，不带标注
      } else {
        el.textContent = utcDate;
      }
    });

    // 更新下拉框状态
    const select = document.getElementById('dpr-timezone-select');
    if (select) {
      select.value = pref;
    }
  }

  // 创建时区切换控件
  function createTimezoneToggle() {
    if (document.getElementById('dpr-timezone-select')) {
      return; // 已经创建过了
    }

    // 找到合适的位置插入控件
    // 优先：在首页"每次日报"模块后面
    // 备选：在页面顶部添加一个浮动控件

    const container = document.createElement('div');
    container.className = 'dpr-timezone-toggle';
    container.innerHTML = `
      <label for="dpr-timezone-select" style="font-size: 0.85rem; color: #666;">
        时区：
        <select id="dpr-timezone-select" style="margin-left: 4px; padding: 2px 6px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.85rem;">
          <option value="beijing">北京时间 (UTC+8)</option>
          <option value="utc">UTC 时间</option>
        </select>
      </label>
    `;

    // 查找插入位置：在首页"每次日报"模块后面
    const dailyReportSection = document.querySelector('.markdown-section');
    if (dailyReportSection) {
      // 在"每次日报"标题后面插入
      const reportTitle = Array.from(dailyReportSection.querySelectorAll('h2, h3')).find(el => el.textContent.includes('每次日报'));
      if (reportTitle && reportTitle.nextSibling) {
        dailyReportSection.insertBefore(container, reportTitle.nextSibling);
      } else {
        dailyReportSection.insertBefore(container, dailyReportSection.firstChild);
      }
    }

    // 绑定切换事件
    const select = document.getElementById('dpr-timezone-select');
    if (select) {
      select.value = getTimezonePref();
      select.addEventListener('change', function(e) {
        const pref = e.target.value;
        const confirmMsg = pref === 'beijing' 
          ? '将切换到北京时间显示，确认？' 
          : '将切换到 UTC 时间显示，确认？';
        
        if (confirm(confirmMsg)) {
          setTimezonePref(pref);
        } else {
          // 恢复原选择
          e.target.value = getTimezonePref();
        }
      });
    }
  }

  // 页面加载时初始化
  function init() {
    applyTimezoneDisplay();
    createTimezoneToggle();
  }

  // 在 DOMContentLoaded 后执行
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 暴露全局函数供外部调用
  window.DPRTimezone = {
    getPref: getTimezonePref,
    setPref: setTimezonePref,
    apply: applyTimezoneDisplay
  };
})();
