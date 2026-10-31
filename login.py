import asyncio
import argparse
import os
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def main(args):
    base = os.getenv("SESSION_DIR", ".")
    session_base = os.path.join(base, args.session)
    client = TelegramClient(session_base, args.api_id, args.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(args.phone)
        if args.send_code_only:
            print("验证码已发送")
            await client.disconnect()
            return
        code = (args.code or input("输入验证码: ").strip()).strip()
        try:
            await client.sign_in(phone=args.phone, code=code)
        except SessionPasswordNeededError:
            pwd = args.password or input("输入二次密码: ").strip()
            await client.sign_in(password=pwd)
    me = await client.get_me()
    print(f"登录成功: {me.id}")
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api_id", type=int, required=True)
    parser.add_argument("--api_hash", type=str, required=True)
    parser.add_argument("--session", type=str, required=True)
    parser.add_argument("--phone", type=str, required=True)
    parser.add_argument("--code", type=str, default=None)
    parser.add_argument("--password", type=str, default=None)
    parser.add_argument("--send_code_only", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(main(args))