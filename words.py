# server.py
from mcp.server.fastmcp import FastMCP
import sys
import logging
import re

logger = logging.getLogger('Words')

# Fix UTF-8 encoding for Windows console
if sys.platform == 'win32':
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

# Create an MCP server
mcp = FastMCP("Words")

def _get_word_list():
    """
    Reads and parses words.md to get a clean list of words.
    """
    try:
        with open('words.md', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Remove date markers and separators
        content = re.sub(r'---\d{4}\.\d{2}\.\d{2}', ' ', content)
        content = content.replace('---', ' ')
        
        # Split by whitespace and filter out empty strings
        word_list = [word for word in content.split() if word]
        return word_list
    except FileNotFoundError:
        logger.error("words.md not found.")
        return []
    except Exception as e:
        logger.error(f"An error occurred while reading words.md: {e}")
        return []

def _get_words_for_practice(start: int, reverse: bool = False) -> dict:
    """Shared logic for retrieving words for practice."""
    word_list = _get_word_list()
    
    if not word_list:
        return {"success": False, "error": "No words found in words.md."}

    if start <= 0:
        return {"success": False, "error": "Start index must be a positive number."}

    # Adjust for 1-based indexing from user
    start_index = start - 1

    if reverse:
        word_list.reverse()

    if start_index >= len(word_list):
        return {"success": False, "error": f"Start index {start} is out of bounds. There are only {len(word_list)} words."}
        
    end_index = start_index + 100
    words_to_practice = word_list[start_index:end_index]
    
    logger.info(f"Returning {len(words_to_practice)} words for practice.")
    return {"success": True, "words": words_to_practice}


@mcp.tool()
def count_words() -> dict:
    """
    words.md是大山的英文生词表，这个工具的功能是告诉智能体生词表里有多少个单词
    """
    logger.info("Calling count_words tool")
    word_list = _get_word_list()
    count = len(word_list)
    logger.info(f"Total words found: {count}")
    return {"success": True, "count": count}

@mcp.tool()
def practice_english_to_chinese(start: int, reverse: bool = False) -> dict:
    """
    Provides 100 English words for English-to-Chinese practice.
    
    :param start: The starting index (1-based) of the words to retrieve.
    :param reverse: If True, retrieves words in reverse order from the end of the file.
    """
    logger.info(f"Calling practice_english_to_chinese with start={start}, reverse={reverse}")
    return _get_words_for_practice(start, reverse)

@mcp.tool()
def practice_chinese_to_english(start: int, reverse: bool = False) -> dict:
    """
    Provides 100 English words for Chinese-to-English practice.
    The agent calling this tool is responsible for translating these English words
    into Chinese and presenting the Chinese meanings to the user.
    
    :param start: The starting index (1-based) of the words to retrieve.
    :param reverse: If True, retrieves words in reverse order from the end of the file.
    """
    logger.info(f"Calling practice_chinese_to_english with start={start}, reverse={reverse}")
    return _get_words_for_practice(start, reverse)

# Start the server
if __name__ == "__main__":
    mcp.run(transport="stdio")
