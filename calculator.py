# server.py
from mcp.server.fastmcp import FastMCP
import sys
import logging

logger = logging.getLogger('Calculator')

# Fix UTF-8 encoding for Windows console
if sys.platform == 'win32':
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

import math
import random

# --- Start of Game24 code ---
class Game24: 
    def __init__(self):
        self.Goal,self.Count=24,0
        #穷举所有的操作符列表共4x4x4=64种
        ops='+-*/'
        self.op_list=[ops[i]+' '+ops[j]+' '+ops[p] for i in range(4) for j in range(4) for p in range(4)]
     
    #得到四个用户输入值
    def getNumbers(self, argv):
        if len(argv) == 1:
            if ' ' in argv[0]:
                argv=argv[0].split()
            else:
                argv=list(argv[0])
        if len(argv) != 4:
            return "参数不对：" + " ".join(argv)

        for i in range(0,4):
            argv[i]=str(argv[i]).lower()
            if argv[i] not in ['1','2','3','4','5','6','7','8','9','10','11','12','13','0','j','q','k','a']:
                return "参数不对：" + argv[i]
            if argv[i] == 'a': argv[i]="1"
            if argv[i] == '0': argv[i]="10"
            if argv[i] == 'j': argv[i]="11"
            if argv[i] == 'q': argv[i]="12"
            if argv[i] == 'k': argv[i]="13"
        return ' '.join(argv)
     
    #穷举所有的数值列表
    def getNumList(self, numbers):
        items=numbers.split()
        data_list = [(items[i]+' '+items[j]+' '+items[p]+' '+items[q]) for i in range(4) for j in range(4) for p in range(4) for q in range(4) if (i != j) &(i != p) &(i != q) &(j != p) &(j != q) &(p != q)]
        return set(data_list)
     
    #计算24点
    def Calc24(self, argv):
        numbers=self.getNumbers(argv)
        if numbers.startswith("参数不对"):
            return numbers
        num_list=self.getNumList(numbers)
        for numlist in num_list:
            nums=numlist.split()
            for oplist in self.op_list:
                ops=oplist.split()
                ret1 = self.Cal24(nums,ops)
                if ret1:
                    return ret1.strip()
        return ''
     
    #对单种运算符顺序和单种数字顺序进行组合运算
    def Cal24(self, nums,op):
        try:
            if round(eval("(("+nums[0]+op[0]+nums[1]+")"+op[1]+nums[2]+")"+op[2]+nums[3]),5) == self.Goal:
                self.Count+=1
                return "(({}{}{}){}{}){}{}={}\n".format(nums[0], op[0], nums[1], op[1], nums[2], op[2], nums[3], self.Goal)
        except:
            pass
        try:
            if round(eval("("+nums[0]+op[0]+nums[1]+")"+op[1]+"("+nums[2]+op[2]+nums[3]+")"), 5) == self.Goal:
                self.Count += 1
                return "({}{}{}){}({}{}{})={}\n".format(nums[0], op[0], nums[1], op[1], nums[2], op[2], nums[3], self.Goal)
        except:
            pass
        try:
            if round(eval("("+nums[0]+op[0]+"("+nums[1]+op[1]+nums[2]+"))"+op[2]+nums[3]), 5) == self.Goal:
                self.Count += 1
                return "({}{}({}{}{})){}{}={}\n".format(nums[0], op[0], nums[1], op[1], nums[2], op[2], nums[3], self.Goal)
        except:
            pass
        try:
            if round(eval(nums[0]+op[0]+"("+nums[1]+op[1]+nums[2]+")"+op[2]+nums[3]+")"), 5) == self.Goal:
                self.Count += 1
                return "{}{}(({}{}{}){}{})={}\n".format(nums[0], op[0], nums[1], op[1], nums[2], op[2], nums[3], self.Goal)
        except:
            pass
        try:
            if round(eval(nums[0]+op[0]+"("+nums[1]+op[1]+"("+nums[2]+op[2]+nums[3]+")))"), 5) == self.Goal:
                self.Count += 1
                return "{}{}({}{}({}{}{}))={}\n".format(nums[0], op[0], nums[1], op[1], nums[2], op[2], nums[3], self.Goal)
        except:
            pass
        return '' 
# --- End of Game24 code ---


# Create an MCP server
mcp = FastMCP("Calculator")

# Add an addition tool
@mcp.tool()
def calculator(python_expression: str) -> dict:
    """For mathamatical calculation, always use this tool to calculate the result of a python expression. You can use 'math' or 'random' directly, without 'import'."""
    result = eval(python_expression, {"math": math, "random": random})
    logger.info(f"Calculating formula: {python_expression}, result: {result}")
    return {"success": True, "result": result}

@mcp.tool()
def game24(numbers: str) -> dict:
    """
    A tool to solve the 24 game. 
    Input 4 characters representing numbers (1-13). 
    The rules are as follows:
    - A single character represents 1-9
    - '0' represents 10
    - 'a' represents 1
    - 'j' represents 11
    - 'q' represents 12
    - 'k' represents 13
    The input must be a string of 4 characters.
    Returns possible solutions or a message if no solution is found.
    """
    logger.info(f"game24: numbers={numbers}")
    game = Game24()
    solutions = game.Calc24([numbers])
    result = solutions if solutions else "No solution found."
    logger.info(f"game24: result={result}")
    return {"success": True, "result": result}

# Start the server
if __name__ == "__main__":
    mcp.run(transport="stdio")