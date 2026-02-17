import subprocess
import sys
from subprocess import PIPE
from threading import Thread
from queue import Queue
import time
import pygame
import pygame.freetype
from pygame.locals import QUIT, KEYDOWN, MOUSEBUTTONDOWN
from threading import Event
import re
from queue import Empty

stop_event = Event()

SELF_PLAY = False   # True：AI自我对抗; False：正常人机

RED_CHESS_COLOR = [255, 255, 255]
BLACK_CHESS_COLOR = [0, 0, 0]
CHESS_NAMES = {'车', '马', '炮', '象', '士', '兵', '帅',
               '俥', '傌', '相', '仕', '卒', '将', '暗'}
ENGINE_UI_EVENT = None
if sys.platform == 'linux':
    font_path = '/usr/share/fonts/truetype/arphic/ukai.ttc'
else:
    font_path = 'C:/Windows/Fonts/simkai.ttf'

play_process = subprocess.Popen(
    [sys.executable, "-u", r"musesfish_pvs_20260215.py"],
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
    text=True,
    errors="replace",
    bufsize=1
)

class ChessInfo:
    def __init__(self, rect, draw_metadata, cmd_pos, is_red=None):
        self.rect = rect
        self.draw_metadata = draw_metadata
        self.cmd_pos = cmd_pos
        self.is_red = is_red


class Board:
    def __init__(self, stdout, font, screen, screen_color, line_color, width=500,
                 footer_font=None, footer_text="按 ESC 键退出"):
        self.stdout = stdout
        self.font = font
        self.screen = screen
        self.screen_color = screen_color
        self.line_color = line_color
        self.width = width
        self.row_spacing = width / 10
        self.col_spacing = width / 8
        self.start_point = 60, 60
        self.start_point_x = self.start_point[0]
        self.start_point_y = self.start_point[1]
        self.chesses = []
        self.empty_chess_rects = []
        self.current_select_chess = None
        self.footer_font = footer_font or font
        self.footer_text = footer_text
        self.footer_height = 30  # 底部栏高度
        self.last_from_cmd = None  # e.g. 'e2'
        self.last_to_cmd = None    # e.g. 'e4'（可选，暂时不用）
        self.game_result = ""   # "" / "红方胜利" / "黑方胜利"
        self.game_over = False
        self.draw_thread_started = False
        self.captured_by_player = []  # 玩家吃到的（黑方）
        self.captured_by_ai = []      # 电脑吃到的（红方）
        # 棋局回顾
        self.snapshots = []          # list[dict]
        self.review_mode = False     # 进入回顾后为 True（冻结引擎输出、禁用走子）
        self.review_idx = -1         # 当前回顾索引（0..len-1）
        # 右下角箭头按钮
        self.arrow_size = 28
        self.arrow_pad = 8
        self.left_arrow_rect = None
        self.right_arrow_rect = None
        self.app_start_ts = time.time()  # 程序启动时间（用于对局总用时）
        self.check_hint_until_ts = 0.0 # 将军提示（中心显示）

    def draw_last_move_marker(self):
        """在上一手走子前的位置画小圆点"""
        if not self.last_from_cmd:
            return
        if len(self.last_from_cmd) != 2:
            return
    
        file_ch = self.last_from_cmd[0]
        rank_ch = self.last_from_cmd[1]
        if not ('a' <= file_ch <= 'i') or (not rank_ch.isdigit()):
            return
    
        col = ord(file_ch) - ord('a') + 1          # a-i -> 1-9
        row = 10 - int(rank_ch)                    # 0-9 -> 10-1
        center, _ = self.get_chess_pos(row, col)
    
        # 画空心圆点
        pygame.draw.circle(self.screen, [0, 0, 255], center, 10, width=2)

    def _strip_ansi(self, s: str) -> str:
        return re.sub(r'\x1b\[[0-9;]*m', '', s)
    
    def _parse_captured_list(self, s: str):
        """
        输入：一整行，例如 '玩家吃子: 车 炮 马(暗)'
        输出：list[ (name, is_dark) ]
        """
        s = self._strip_ansi(s).strip()
        if ':' not in s:
            return []
        rhs = s.split(':', 1)[1].strip()
        if not rhs:
            return []
    
        items = []
        for tok in rhs.split():
            is_dark = '(暗)' in tok
            name = tok.replace('(暗)', '').strip()
            if name:
                items.append((name, is_dark))
        return items

    def draw_captured_area(self):
        """棋盘下方两行显示双方被吃掉的棋子"""
        w, h = self.screen.get_size()
    
        # 棋盘底边 y
        board_bottom = self.start_point_y + self.row_spacing * 9
    
        # 吃子区布局
        margin_x = 12
        y1 = int(board_bottom + 52)
        y2 = int(y1 + 40)  # 第二行
    
        r = 16            # 吃子棋子半径
        step = 2 * r + 6  # 横向间距
    
        # 画背景条
        area_h = 40 * 2 + 12
        area_rect = pygame.Rect(0, y1 - 8, w, area_h)
        pygame.draw.rect(self.screen, [245, 245, 245],
                         area_rect)
    
        # 行标题
        self.footer_font.render_to(self.screen, (margin_x, y1 - 6), "玩家吃子:", [0, 0, 0])
        self.footer_font.render_to(self.screen, (margin_x, y2 - 6), "电脑吃子:", [0, 0, 0])
    
        # 起始 x：留出标题位置
        pad = 30
        w1 = self.footer_font.get_rect("玩家吃子:").width
        w2 = self.footer_font.get_rect("电脑吃子:").width
        x0 = margin_x + max(w1, w2) + pad
    
        def _draw_cap_piece(cx, cy, chess_color, name, is_dark, show_name=True):
            # 边框
            border_w = 1
            border_color = [255, 255, 255] if chess_color == BLACK_CHESS_COLOR else [0, 0, 0]
            pygame.draw.circle(self.screen, border_color, (cx, cy), r)
    
            # 填充
            pygame.draw.circle(self.screen, chess_color, (cx, cy), r - border_w)
    
            # 暗子标记：右上角小点
            if is_dark:
                pygame.draw.circle(self.screen, [0, 0, 255], (cx + r - 5, cy - r + 5), 6, 0)
    
            # 文字（玩家背吃暗子不写字）
            if (not show_name) or (not name) or (name == '暗'):
                return
    
            if chess_color == BLACK_CHESS_COLOR:
                font_color = [255, 255, 255]
            elif chess_color == RED_CHESS_COLOR:
                font_color = [255, 0, 0]
            else:
                font_color = [0, 0, 0]
    
            text_rect = self.font.get_rect(name)
            text_rect.center = (cx, cy)
            self.font.render_to(self.screen, text_rect.topleft, name, font_color)
    
        # 1) 玩家吃到的：黑方棋子，暗子要显示真实身份+暗子标记
        x = x0
        for name, is_dark in self.captured_by_player:
            if x + r > w - margin_x:
                break  # 一行显示不下就截断
            _draw_cap_piece(x, y1 + 18, BLACK_CHESS_COLOR, name, is_dark, show_name=True)
            x += step
    
        # 2) 电脑吃到的：红方棋子；若暗子，则画空白棋但仍标记暗子
        x = x0
        for name, is_dark in self.captured_by_ai:
            if x + r > w - margin_x:
                break
            if is_dark:
                _draw_cap_piece(x, y2 + 18, RED_CHESS_COLOR, "", True, show_name=False)  # 空白但保留暗子标记
            else:
                _draw_cap_piece(x, y2 + 18, RED_CHESS_COLOR, name, False, show_name=True)
            x += step
        
        return area_rect

    def redraw_captured_strip(self):
        area_rect = self.draw_captured_area()
        self.draw_footer()
        # 只更新吃子区 + 底部 footer，立刻呈现
        w, h = self.screen.get_size()
        footer_rect = pygame.Rect(0, h - self.footer_height, w, self.footer_height)
        pygame.display.update([area_rect, footer_rect])

    def _format_elapsed_mm_ss(self, seconds: float) -> str:
        total = int(seconds)
        mm = total // 60
        ss = total % 60
        return f"{mm:02d}:{ss:02d}"   # 分钟可能超过 99，会自动变长

    def draw_footer(self):
        w, h = self.screen.get_size()
        y0 = h - self.footer_height
        pygame.draw.rect(self.screen, [245, 245, 245], pygame.Rect(0, y0, w, self.footer_height))

        base = "按 ESC 键退出"
        x = 10
        y = y0 + self.footer_height // 2

        # 先画基础文字（黑色）
        rect_base = self.footer_font.get_rect(base)
        rect_base.midleft = (x, y)
        self.footer_font.render_to(self.screen, rect_base.topleft, base, [0, 0, 0])
        x += rect_base.width + 20

        # 需要展示胜负时
        if self.game_result:
            # 对局结束后：追加用时
            if self.game_over:
                elapsed = time.time() - self.app_start_ts
                elapsed_str = self._format_elapsed_mm_ss(elapsed)
                result_text = f"{self.game_result}    用时 {elapsed_str}"
            else:
                result_text = self.game_result

            # 如果是红方胜利：“红方胜利！”这四个字用红色渲染，其它文字黑色
            if self.game_result.startswith("红方胜利"):
                win_red = "红方胜利！"
                # 先画红色的“红方胜利！”
                rect_win = self.footer_font.get_rect(win_red)
                rect_win.midleft = (x, y)
                self.footer_font.render_to(self.screen, rect_win.topleft, win_red, [255, 0, 0])
                x += rect_win.width

                # 再画剩余部分
                rest = result_text[len(win_red):]
                if rest:
                    rect_rest = self.footer_font.get_rect(rest)
                    rect_rest.midleft = (x, y)
                    self.footer_font.render_to(self.screen, rect_rest.topleft, rest, [0, 0, 0])
                    x += rect_rest.width
            else:
                # 黑方胜利或其它：全部黑色
                rect_res = self.footer_font.get_rect(result_text)
                rect_res.midleft = (x, y)
                self.footer_font.render_to(self.screen, rect_res.topleft, result_text, [0, 0, 0])
                x += rect_res.width

        # 对局结束后画左右箭头
        self.draw_review_arrows()

    def trigger_check_hint(self):
        """触发“将军！”提示，持续几秒"""
        self.check_hint_until_ts = time.time() + 2.0

    def is_check_hint_active(self) -> bool:
        return time.time() < self.check_hint_until_ts

    def draw_check_hint(self):
        """在棋盘中央绘制“将军！”"""
        if not self.is_check_hint_active():
            return

        w, h = self.screen.get_size()

        # 棋盘中心（start_point 到 width）
        cx = int(self.start_point_x + self.width / 2)
        cy = int(self.start_point_y + (self.row_spacing * 9) / 2)

        text = "将军！"

        # 可选做一个半透明背景块（让字更醒目）
        # 需要 SRCALPHA surface
        # overlay_w, overlay_h = 220, 90
        # overlay = pygame.Surface((overlay_w, overlay_h), pygame.SRCALPHA)
        # overlay.fill((0, 0, 0, 120))  # 黑色半透明
        # overlay_rect = overlay.get_rect(center=(cx, cy))
        # self.screen.blit(overlay, overlay_rect.topleft)

        # 画大字：更大一号更醒目
        big_font = pygame.freetype.Font(font_path, 64)
        big_font.strong = True
        big_font.antialiased = True

        rect = big_font.get_rect(text)
        rect.center = (cx, cy)
        big_font.render_to(self.screen, rect.topleft, text, (255, 255, 0))  # 黄色字

    def draw_board(self):
        x, y = self.start_point
        for i in range(10):
            pygame.draw.line(self.screen, self.line_color, [x, y],
                             [x + self.width, y])
            y += self.row_spacing

        x, y = self.start_point
        for i in range(9):
            pygame.draw.line(self.screen, self.line_color, [x, y],
                             [x, y + self.row_spacing * 9])
            x += self.col_spacing

        x, y = self.start_point_x + self.col_spacing, \
               self.start_point_y + self.row_spacing * 4
        for i in range(7):
            pygame.draw.line(self.screen, self.screen_color, [x, y],
                             [x, y + self.row_spacing])
            x += self.col_spacing
        # ----------------------------
        # 九宫格“米”字两条斜线（上、下各一组）
        # 九宫格列：4~6（对应 col=4..6），行：上 1~3，下 8~10
        # ----------------------------
        def p(row, col):
            # 返回该格点中心坐标（与棋子中心一致）
            center, _ = self.get_chess_pos(row, col)
            return center

        # 上九宫（行 1~3）
        pygame.draw.line(self.screen, self.line_color, p(1, 4), p(3, 6))
        pygame.draw.line(self.screen, self.line_color, p(1, 6), p(3, 4))

        # 下九宫（行 8~10）
        pygame.draw.line(self.screen, self.line_color, p(8, 4), p(10, 6))
        pygame.draw.line(self.screen, self.line_color, p(8, 6), p(10, 4))

    def get_chess_pos(self, row, col):
        center = [self.start_point_x + self.col_spacing * (col - 1),
                  self.start_point_y + self.row_spacing * (row - 1)]
        radius = self.row_spacing / 2
        return center, radius

    def draw_a_chess(self, row, col, chess_color, chess_name):
        center, radius = self.get_chess_pos(row, col)
    
        # ---------- 外圈边框 ----------
        border_w = 1
        border_color = [255, 255, 255] if chess_color == BLACK_CHESS_COLOR else [0, 0, 0]
    
        # 外圈（边框圆）
        pygame.draw.circle(self.screen, border_color, center, int(radius))
    
        # 内圈（填充圆）
        inner_r = max(1, int(radius - border_w))
        rect = pygame.draw.circle(self.screen, chess_color, center, inner_r)
    
        # ---------- 花纹同心圆（空心） ----------
        # 半径比棋子略小；线色与边框一致；线宽可调
        pattern_r = max(1, int(inner_r * 0.92))
        pattern_w = 1                             # 花纹线宽
        pygame.draw.circle(self.screen, border_color, center, pattern_r, width=pattern_w)
    
        # ---------- 暗子只画圆不写字 ----------
        if chess_name == '暗':
            return rect
    
        # 文字颜色
        if chess_color == BLACK_CHESS_COLOR:
            font_color = [255, 255, 255]
        elif chess_color == RED_CHESS_COLOR:
            font_color = [255, 0, 0]
        else:
            font_color = [0, 0, 0]
    
        text_rect = self.font.get_rect(chess_name)
        text_rect.center = (center[0], center[1])
        self.font.render_to(self.screen, text_rect.topleft, chess_name, font_color)
        return rect


    def draw(self):
        while True:
            try:
                line = self.stdout.get()   # 阻塞，没数据就睡着，不耗CPU
            except Empty:
                continue
    
            if not line:
                continue
    
            raw = line
            s = raw.strip()
            if not s:
                continue

            if self.review_mode:
                continue
            plain = self._strip_ansi(s)
            # --------- 将军检测：出现即触发提示 ---------
            # 兼容中英文：含“将军”或含“check”
            if ("将军" in plain) or re.search(r"\bcheck\b", plain, re.IGNORECASE):
                self.trigger_check_hint()
                # 立刻刷新一帧，让“将军！”马上出现
                self.redraw_all()
                continue

            MOVE_RE = re.compile(r"My move:\s*([a-i][0-9][a-i][0-9])")
            m = MOVE_RE.search(s)
            if m:
                mv = m.group(1)          # 例如 'h2e2'
                self.last_from_cmd = mv[:2]
                self.last_to_cmd = mv[2:4]

            # 解析吃子列表（来自引擎输出）
            if plain.startswith("玩家吃子"):
                self.captured_by_player = self._parse_captured_list(plain)
                self.redraw_captured_strip()            
            if plain.startswith("电脑吃子"):
                self.captured_by_ai = self._parse_captured_list(plain)


            # 胜负检测（来自引擎输出）
            if "You win!" in s:
                self.game_result = "红方胜利！"
                self.game_over = True
                # 确保把终局也记录进快照（即使终局没有再打印棋盘）
                self.push_snapshot(force=True)
                self.review_idx = len(self.snapshots) - 1
                self.redraw_all()
                continue
            
            elif ("You lost" in s) or ("You lose" in s):
                self.game_result = "黑方胜利！"
                self.game_over = True
                self.push_snapshot(force=True)
                self.review_idx = len(self.snapshots) - 1
                self.redraw_all()
                continue

            elif "Checkmate" in s:
                # 保险：如果引擎打印 Checkmate，但没区分谁赢，可以按最近语句再细化
                pass

            #  只要以“电脑吃子:”开头就触发画棋盘
            if s.startswith('电脑吃子'):
                self.screen.fill(self.screen_color)
                self.draw_board()
                self.chesses = []
                self.empty_chess_rects = []
                self.current_select_chess = None
            
                # 画吃子区
                self.draw_captured_area()
            
                self.draw_last_move_marker()
                self.draw_footer()
                # 叠加“将军！”提示
                self.draw_check_hint()

                pygame.display.update()
                continue
    
            #  解析棋盘行：strip 后首字符是数字才处理
            if not s or (not s[0].isdigit()):
                continue
    
            raw = line  # 保留原始（含ANSI）
            s = raw.strip()
            if not s or (not s[0].isdigit()):
                continue
            
            row_digit, cells = parse_row_cells(raw)
            row = 10 - int(row_digit)
            col = 1
            
            for name, is_red in cells:
                # 只处理空位点/棋子名；其它字符忽略
                if name == '．':
                    center, radius = self.get_chess_pos(row, col)
                    self.empty_chess_rects.append(
                        ChessInfo(
                            pygame.Rect(center[0] - radius, center[1] - radius, radius * 2, radius * 2),
                            None,
                            f'{chr(col + 96)}{row_digit}'
                        )
                    )
                    col += 1
                    continue
            
                if name not in CHESS_NAMES:
                    continue
            
                chess_color = RED_CHESS_COLOR if is_red else BLACK_CHESS_COLOR
                params = row, col, chess_color, name
                chess_rect = self.draw_a_chess(*params)
                self.chesses.append(
                    ChessInfo(chess_rect, params, f'{chr(col + 96)}{row_digit}', is_red=is_red)
                )                
                col += 1
            
            # 不要每行都 update
            if row_digit == '0':   # 最后一行到齐了
                self.draw_last_move_marker()
                self.draw_footer()
                # 叠加“将军！”提示
                self.draw_check_hint()

                pygame.display.update()

                # 记录快照（用于对局结束后回顾）
                self.push_snapshot()



    def select(self, chess):
        self.redraw_all()
        params = list(chess.draw_metadata)
        params[2] = [128, 128, 128]   # 灰色高亮
        self.draw_a_chess(*params)
        self.current_select_chess = chess
        self.draw_last_move_marker()
        self.draw_footer()
        pygame.display.update()

    def move(self, pos):
        global play_process
    
        # 游戏已结束：不再允许走子
        if self.game_over:
            return
    
        # 引擎已退出：不再写 stdin
        if play_process.poll() is not None:
            self.game_over = True
            return
    
        # 记录上一手起点
        if pos and len(pos) >= 4:
            self.last_from_cmd = pos[:2]
            self.last_to_cmd = pos[2:4]
    
        try:
            play_process.stdin.write(pos + '\n')
            play_process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            # BrokenPipe: 子进程已退出
            # OSError: Win 下常见 Invalid argument（stdin 已不可用）
            self.game_over = True
            return

    # =========================
    #  回顾：快照/渲染/箭头
    # =========================
    def _cmd_to_rowcol(self, cmd_pos: str):
        # cmd_pos: 'a0'..'i9'
        if (not cmd_pos) or len(cmd_pos) != 2:
            return None
        file_ch, rank_ch = cmd_pos[0], cmd_pos[1]
        if not ('a' <= file_ch <= 'i') or (not rank_ch.isdigit()):
            return None
        col = ord(file_ch) - ord('a') + 1          # 1..9
        row_digit = rank_ch                          # '0'..'9'
        row = 10 - int(row_digit)                    # 1..10
        return row, col

    def _make_snapshot(self):
        """从当前 UI 状态生成一个可回放快照"""
        pieces = []
        for c in self.chesses:
            if not c.draw_metadata:
                continue
            # draw_metadata = (row, col, chess_color, chess_name)
            name = c.draw_metadata[3]
            pieces.append((c.cmd_pos, name, bool(c.is_red)))

        snap = {
            "pieces": pieces,  # list[(cmd_pos, name, is_red)]
            "captured_by_player": list(self.captured_by_player),
            "captured_by_ai": list(self.captured_by_ai),
            "last_from_cmd": self.last_from_cmd,
            "last_to_cmd": self.last_to_cmd,
            "game_result": self.game_result,
        }
        return snap

    def _same_snapshot(self, a, b):
        if (a is None) or (b is None):
            return False
        # 只比关键字段，避免无意义重复
        return (a.get("pieces") == b.get("pieces")
                and a.get("captured_by_player") == b.get("captured_by_player")
                and a.get("captured_by_ai") == b.get("captured_by_ai")
                and a.get("last_from_cmd") == b.get("last_from_cmd")
                and a.get("game_result") == b.get("game_result"))

    def push_snapshot(self, force=False):
        snap = self._make_snapshot()
        if (not force) and self.snapshots and self._same_snapshot(self.snapshots[-1], snap):
            return
        self.snapshots.append(snap)
        self.review_idx = len(self.snapshots) - 1

    def load_snapshot(self, idx: int):
        """把第 idx 个快照加载回当前 UI 状态（不触发引擎）"""
        if idx < 0 or idx >= len(self.snapshots):
            return
        snap = self.snapshots[idx]

        # 清空并重建棋子/空位 rect
        self.chesses = []
        self.empty_chess_rects = []
        self.current_select_chess = None

        # 恢复辅助信息
        self.captured_by_player = list(snap["captured_by_player"])
        self.captured_by_ai = list(snap["captured_by_ai"])
        self.last_from_cmd = snap.get("last_from_cmd", None)
        self.last_to_cmd = snap.get("last_to_cmd", None)
        self.game_result = snap.get("game_result", self.game_result)

        # piece_map: cmd_pos -> (name, is_red)
        piece_map = {cmd: (name, is_red) for (cmd, name, is_red) in snap["pieces"]}

        for row_digit_int in range(9, -1, -1):  # 9..0
            row_digit = str(row_digit_int)
            row = 10 - row_digit_int
            for col in range(1, 10):
                cmd = f"{chr(col + 96)}{row_digit}"
                if cmd in piece_map:
                    name, is_red = piece_map[cmd]
                    chess_color = RED_CHESS_COLOR if is_red else BLACK_CHESS_COLOR
                    params = (row, col, chess_color, name)
                    chess_rect = self.draw_a_chess(*params)
                    self.chesses.append(ChessInfo(chess_rect, params, cmd, is_red=is_red))
                else:
                    center, radius = self.get_chess_pos(row, col)
                    self.empty_chess_rects.append(
                        ChessInfo(
                            pygame.Rect(center[0] - radius, center[1] - radius, radius * 2, radius * 2),
                            None,
                            cmd
                        )
                    )

        self.redraw_all()

    def draw_review_arrows(self):
        """右下角左右箭头（仅对局结束后显示）"""
        if not self.game_over:
            self.left_arrow_rect = None
            self.right_arrow_rect = None
            return

        w, h = self.screen.get_size()
        y0 = h - self.footer_height

        size = self.arrow_size
        pad = self.arrow_pad
        gap = 6

        # 放在 footer 右侧区域
        right = w - pad
        self.right_arrow_rect = pygame.Rect(right - size, y0 + (self.footer_height - size) // 2, size, size)
        self.left_arrow_rect = pygame.Rect(right - size * 2 - gap, y0 + (self.footer_height - size) // 2, size, size)

        # 根据是否可前进/后退决定“可用/不可用”外观
        can_left = (self.review_idx > 0)
        can_right = (self.review_idx < len(self.snapshots) - 1)

        def _draw_btn(rect, enabled):
            bg = [230, 230, 230] if enabled else [200, 200, 200]
            pygame.draw.rect(self.screen, bg, rect, border_radius=6)
            pygame.draw.rect(self.screen, [80, 80, 80], rect, width=1, border_radius=6)

        _draw_btn(self.left_arrow_rect, can_left)
        _draw_btn(self.right_arrow_rect, can_right)

        # 画三角形箭头
        def _tri_left(rect, enabled):
            cx, cy = rect.center
            s = rect.width // 3
            color = [30, 30, 30] if enabled else [120, 120, 120]
            pts = [(cx + s, cy - s), (cx + s, cy + s), (cx - s, cy)]
            pygame.draw.polygon(self.screen, color, pts)

        def _tri_right(rect, enabled):
            cx, cy = rect.center
            s = rect.width // 3
            color = [30, 30, 30] if enabled else [120, 120, 120]
            pts = [(cx - s, cy - s), (cx - s, cy + s), (cx + s, cy)]
            pygame.draw.polygon(self.screen, color, pts)

        _tri_left(self.left_arrow_rect, can_left)
        _tri_right(self.right_arrow_rect, can_right)

    def handle_review_click(self, mouse_pos):
        """对局结束后：处理左右箭头点击。返回 True 表示已处理。"""
        if not self.game_over:
            return False
        if not self.left_arrow_rect or not self.right_arrow_rect:
            return False

        if self.left_arrow_rect.collidepoint(mouse_pos):
            self.step_review(-1)
            return True
        if self.right_arrow_rect.collidepoint(mouse_pos):
            self.step_review(+1)
            return True
        return False

    def step_review(self, delta: int):
        """delta=-1 后退，+1 前进（不触发引擎）"""
        if not self.snapshots:
            return
        # 一旦开始回顾，冻结引擎输出与走子
        self.review_mode = True

        ni = self.review_idx + delta
        ni = max(0, min(ni, len(self.snapshots) - 1))
        if ni == self.review_idx:
            return

        self.review_idx = ni
        self.load_snapshot(self.review_idx)


    def redraw_all(self):
        self.screen.fill(self.screen_color)
        self.draw_board()
    
        for c in self.chesses:
            if c.draw_metadata:
                self.draw_a_chess(*c.draw_metadata)
    
        # 吃子区
        self.draw_captured_area()
    
        self.draw_last_move_marker()
        self.draw_footer()
        self.draw_check_hint()
        pygame.display.update()

    def deselect(self):
        self.current_select_chess = None
        self.redraw_all()

PRINT_ENGINE = True
PRINT_BOARD = False

def is_board_row(line: str) -> bool:
    s = line.strip()
    # 棋盘行格式是：'9 车马...' —— 数字后面紧跟一个空格
    # 纯数字分数行如 '3696' 没有空格，不应被当成棋盘行
    return re.match(r'^[0-9]\s', s) is not None

def read_stdout(result: Queue, play_process):
    with open("engine_stdout.log", "w", encoding="utf-8", errors="replace") as f:
        time.sleep(0.2)
        while not stop_event.is_set():
            if play_process.poll() is not None:
                if PRINT_ENGINE:
                    print("[ENGINE] exited rc=", play_process.returncode, flush=True)
                return
            line = play_process.stdout.readline()
            if line:
                f.write(line); f.flush()

                plain = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                if plain == "ａｂｃｄｅｆｇｈｉ":
                    result.put(line)
                    continue

                if PRINT_ENGINE and (PRINT_BOARD or not is_board_row(line)):
                    s = line.strip()
                    if re.fullmatch(r"-?\d+", s):
                        print(s, flush=True)
                    else:
                        print("[ENGINE-OUT]", line.rstrip("\n"), flush=True)

                result.put(line)


def read_stderr(play_process):
    with open("engine_stderr.log", "w", encoding="utf-8", errors="replace") as f:
        while not stop_event.is_set():
            if play_process.poll() is not None:
                return
            line = play_process.stderr.readline()
            if line:
                f.write(line); f.flush()
                print("[ENGINE-ERR]", line.rstrip("\n"), flush=True)  #  直接显示

ANSI_RED = '\x1b[31m'
ANSI_RESET = '\x1b[0m'

def parse_row_cells(raw_line: str):
    """
    输入：print_pos 打印出来的一整行（strip后，形如: '9 车马... 或含 \x1b[31m俥\x1b[0m'）
    输出：row_digit(str), cells(list[(ch, is_red)])
    """
    s = raw_line.strip()
    row_digit = s[0]
    rest = s[1:].lstrip()

    cells = []
    i = 0
    while i < len(rest):
        # 跳过空格
        if rest[i].isspace():
            i += 1
            continue

        # 红方棋子：\x1b[31m + 字符 + \x1b[0m
        if rest.startswith(ANSI_RED, i):
            i += len(ANSI_RED)
            if i < len(rest):
                ch = rest[i]
                cells.append((ch, True))
                i += 1
            # 跳过 reset
            if rest.startswith(ANSI_RESET, i):
                i += len(ANSI_RESET)
            continue

        # 其它 ANSI 序列（保险起见跳过）
        if rest[i] == '\x1b':
            m = rest.find('m', i)
            i = (m + 1) if m != -1 else (i + 1)
            continue

        # 普通字符（黑方棋子/空位点）
        cells.append((rest[i], False))
        i += 1

    return row_digit, cells

def safe_quit(play_process):
    stop_event.set()
    try:
        pygame.quit()
    except:
        pass

    # 先温和结束子进程
    try:
        play_process.terminate()
    except:
        pass

    # 等一会儿，不行再强杀
    try:
        play_process.wait(timeout=1.5)
    except:
        try:
            play_process.kill()
        except:
            pass

def main():
    stdout = Queue(1024)
    Thread(target=read_stdout, args=(stdout, play_process),
           daemon=True).start()
    Thread(target=read_stderr, args=(play_process,), daemon=True).start()
    print("[MAIN] engine pid =", play_process.pid, flush=True)
    time.sleep(0.3)
    if play_process.poll() is not None:
        print("[MAIN] engine exited early rc =", play_process.returncode, flush=True)

    # 初始化pygame
    pygame.init()
    global ENGINE_UI_EVENT
    ENGINE_UI_EVENT = pygame.USEREVENT + 1
    CHECK_HINT_EVENT = pygame.USEREVENT + 2
    pygame.time.set_timer(CHECK_HINT_EVENT, 200)  # 200ms 检查一次即可

    # 获取对显示系统的访问，并创建一个窗口screen
    font = pygame.freetype.Font(font_path, 30)
    font.strong = True
    font.antialiased = True
    width = 670
    height = 670
    screen = pygame.display.set_mode((width, height))
    screen_color = [238, 154, 73]  # 设置画布颜色,[238,154,73]对应为棕黄色
    line_color = [0, 0, 0]  # 设置线条颜色，[0,0,0]对应黑色

    board = Board(stdout, font, screen, screen_color, line_color)
    screen.fill(screen_color)  # 清屏
    board.redraw_all()
    if not board.draw_thread_started:
        board.draw_thread_started = True
        Thread(target=board.draw, daemon=True).start()

    while not stop_event.is_set():
        event = pygame.event.wait()
        events = [event] + pygame.event.get()  # 把已经排队的也一口气处理掉
        for event in events:
            if event.type == QUIT:
                safe_quit(play_process)
                return
            elif event.type == KEYDOWN:
                # ESC 退出
                if event.key == pygame.K_ESCAPE:
                    safe_quit(play_process)
                    return
            elif event.type == ENGINE_UI_EVENT:
                # 只刷新吃子区（立刻把玩家刚吃的子显示出来）
                if getattr(event, "kind", "") == "cap":
                    board.redraw_captured_strip()
                continue
            elif event.type == CHECK_HINT_EVENT:
                # 如果提示刚好过期，需要刷新把字擦掉
                # 做法：只要提示不再 active，但之前曾经 active（until_ts>0）就刷新一次
                if (board.check_hint_until_ts > 0) and (not board.is_check_hint_active()):
                    board.check_hint_until_ts = 0  # 清零，避免重复刷新
                    board.redraw_all()
                continue
            elif event.type == MOUSEBUTTONDOWN:
                if event.button != 1:
                    continue
                # 自我对抗模式：对局未结束时，禁用玩家所有点击走子/选子
                if SELF_PLAY and (not board.game_over):
                    continue

                # 对局结束后：只允许点左右箭头回顾
                if board.game_over:
                    if board.handle_review_click(event.pos):
                        continue
                    else:
                        continue
            
                clicked_pos = event.pos
            
                # 1) 先判断有没有点到某个棋子
                clicked_chess = None
                for chess in board.chesses:
                    if chess.rect.collidepoint(clicked_pos):
                        clicked_chess = chess
                        break
            
                # clicked_chess != None 表示点到某个棋子
                if clicked_chess is not None:
                    if board.current_select_chess is None:
                        board.select(clicked_chess)
                    else:
                        # 已选中棋子
                        if clicked_chess is board.current_select_chess:
                            # 再点同一个 -> 取消
                            board.deselect()
                        else:
                            # 判断阵营：同阵营=切换选中；异阵营=尝试吃子
                            if (clicked_chess.is_red == board.current_select_chess.is_red):
                                board.select(clicked_chess)   # 切换选中
                            else:
                                #  吃子：目标就是对方棋子的格子 cmd_pos
                                # 1) 先把“被吃掉的棋子”立刻显示到吃子区（不等引擎思考完）
                                cap_name = clicked_chess.draw_metadata[3]  # 棋子名
                                cap_is_dark = (cap_name == '暗')           # 只要当下还是暗子，就标记为暗
                                
                                # 这里先用 UI 看到的名字：明子=真实字；暗子=显示“暗”(之后引擎输出会覆盖成真实列表)
                                board.captured_by_player.append((cap_name, cap_is_dark))
                                board.redraw_captured_strip()
                                
                                # 2) 再把走子命令发给引擎
                                board.move(f'{board.current_select_chess.cmd_pos}{clicked_chess.cmd_pos}')
                                board.current_select_chess = None

                    continue
            
                # 2) 没点到棋子：如果当前有选中，再看是否点到了可落子空位
                if board.current_select_chess is not None:
                    clicked_empty = None
                    for chess in board.empty_chess_rects:
                        if chess.rect.collidepoint(clicked_pos):
                            clicked_empty = chess
                            break
            
                    if clicked_empty is not None:
                        board.move(f'{board.current_select_chess.cmd_pos}{clicked_empty.cmd_pos}')
                        board.current_select_chess = None  # 走完自动取消
                    else:
                        # c) 点到其他空白区域 -> 取消选择
                        board.deselect()



if __name__ == '__main__':
    main()
