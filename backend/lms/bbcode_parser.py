import re
import random

class BBBienParser:
    """
    Thư viện Python chuyển dịch từ PHP (Trần Hữu Nam - huunam0@gmail.com)
    Hỗ trợ sinh biến ngẫu nhiên, biểu thức điều kiện, hoán vị xâu/mảng trực tiếp trên server Django.
    """
    def __init__(self, seed=None):
        self.bb_bien = {}
        self.seed = seed

    def parse(self, text):
        if not text:
            return ""
            
        if self.seed is not None:
            # Lưu trạng thái random hiện tại của hệ thống để khôi phục lại sau khi parse
            state = random.getstate()
            random.seed(self.seed)
        
        try:
            # 1. Phân tích cú pháp trên toàn bộ văn bản (bao gồm cả khối ẩn [hide] và khối xóa [xoa])
            text = self._parse_loop(text)
            
            # 2. Khối [hide]...[/hide] được giữ lại trên client nhưng ẩn đi bằng CSS
            text = re.sub(r'\[hide\](.*?)\[/hide\]', r'<span style="display:none;">\1</span>', text, flags=re.DOTALL)
            
            # 3. Khối [xoa]...[/xoa] bị xóa hoàn toàn ở phía server, không chuyển xuống client
            text = re.sub(r'\[xoa\](.*?)\[/xoa\]', "", text, flags=re.DOTALL)
            
            return text
        finally:
            if self.seed is not None:
                random.setstate(state)

    def _parse_loop(self, text):
        # Biểu thức chính quy tìm các thẻ shortcode cơ bản
        pattern = r'\[(chonso|chon0|chon|bien|bthuc|ngau|dsdao|xaudao|dssx)\s*([^\[\]]*)\]'
        
        iterations = 0
        while iterations < 100:  # Giới hạn 100 vòng tránh lặp vô hạn
            match = re.search(pattern, text)
            if not match:
                break
                
            full_tag = match.group(0)
            code = match.group(1)
            arg = match.group(2)
            
            result = self._evaluate_code(code, arg)
            text = text.replace(full_tag, str(result), 1)
            iterations += 1
            
        return text

    def _evaluate_code(self, code, arg):
        if code == "bien":
            # Kiểm tra xem có phải cú pháp định nghĩa: ví dụ [bien1=a,b,c]
            def_match = re.match(r'^([0-9]+)=(.*)$', arg, re.DOTALL)
            if def_match:
                new_id = int(def_match.group(1))
                danhsach = [x.strip() for x in def_match.group(2).split(',')]
                
                # Ràng buộc: giá trị các biến không trùng nhau (chọn phần tử chưa được dùng)
                already_used = list(self.bb_bien.values())
                filtered = [x for x in danhsach if x not in already_used]
                if not filtered:
                    filtered = danhsach  # Fallback nếu đã hết lựa chọn
                
                val = random.choice(filtered)
                self.bb_bien[new_id] = val
                return val
            else:
                # Truy xuất giá trị biến: ví dụ [bien1]
                try:
                    var_id = int(arg.strip())
                    return self.bb_bien.get(var_id, "")
                except ValueError:
                    return ""
                    
        elif code == "chon":
            danhsach = [x.strip() for x in arg.split(',')]
            return random.choice(danhsach) if danhsach else ""
            
        elif code == "chon0":
            danhsach = [x.strip() for x in arg.split(',')] + [""]
            return random.choice(danhsach)
            
        elif code == "chonso":
            danhsach0 = [x.strip() for x in arg.split(',')]
            danhsach = []
            for ptu in danhsach0:
                mut = ptu.split('-')
                if len(mut) == 2 and mut[0].isdigit() and mut[1].isdigit():
                    for i in range(int(mut[0]), int(mut[1]) + 1):
                        danhsach.append(str(i))
                else:
                    danhsach.append(ptu)
            return random.choice(danhsach) if danhsach else ""
            
        elif code == "bthuc":
            return self._eval_ternary(arg)
            
        elif code == "ngau":
            danhsach = [x.strip() for x in arg.split(',')]
            random.shuffle(danhsach)
            return ", ".join(danhsach)
            
        elif code == "dsdao":
            phan = ", "
            if len(arg) > 2 and arg[1] == ' ' and not arg[0].isalnum():
                phan = arg[0]
                arg = arg[2:]
            danhsach = [x.strip() for x in arg.split(',')]
            random.shuffle(danhsach)
            return phan.join(danhsach)
            
        elif code == "xaudao":
            chars = list(arg)
            random.shuffle(chars)
            return "".join(chars)
            
        elif code == "dssx":
            danhsach = [x.strip() for x in arg.split(',')]
            danhsach.sort()
            return ", ".join(danhsach)
            
        return ""

    def _eval_ternary(self, s):
        """
        Phân tích đệ quy biểu thức chứa toán tử điều kiện (ternary) C-style: `A ? B : C`
        """
        s = s.strip()
        if '?' not in s:
            return self._safe_eval(s)
            
        depth = 0
        q_idx = -1
        for i, char in enumerate(s):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == '?' and depth == 0:
                q_idx = i
                break
                
        if q_idx == -1:
            if s.startswith('(') and s.endswith(')'):
                return self._eval_ternary(s[1:-1])
            q_idx = s.find('?')
            if q_idx == -1:
                return self._safe_eval(s)
                
        depth = 0
        c_idx = -1
        for i in range(q_idx + 1, len(s)):
            char = s[i]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ':' and depth == 0:
                c_idx = i
                break
                
        if c_idx == -1:
            return self._safe_eval(s)
            
        cond = s[:q_idx]
        val_true = s[q_idx+1:c_idx]
        val_false = s[c_idx+1:]
        
        if self._eval_ternary(cond):
            return self._eval_ternary(val_true)
        else:
            return self._eval_ternary(val_false)

    def _safe_eval(self, expr):
        """
        Đánh giá an toàn biểu thức logic toán học cơ bản không dùng eval nguy hiểm.
        """
        expr = expr.strip()
        if not expr:
            return ""
        if expr.lower() == 'true':
            return True
        if expr.lower() == 'false':
            return False
            
        # Dịch các toán tử logic PHP sang Python
        expr = expr.replace('&&', ' and ').replace('||', ' or ')
        allowed_names = {
            'abs': abs, 'round': round, 'max': max, 'min': min,
            'True': True, 'False': False
        }
        try:
            return eval(expr, {"__builtins__": {}}, allowed_names)
        except Exception:
            return expr
