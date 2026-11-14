"""Assemble the managed desktop validation app; users launch only Qiban.exe.

The desktop input is copied with its own ``desktop-sources.zip`` and ``desktop-build.json``, which
record the shell's local sources and the published executable of that same ``build.ps1`` run. Both
the input directory and the assembled one are verified, so a program that does not match the
recorded build is refused before any output is written and again after the copy. The record keeps
the published file name ``Qiban.DesktopProbe.exe``; ``Qiban.exe`` is that same file renamed with
unchanged bytes, and ``uninstall.exe`` is one byte-identical copy of it. The alias is never a second
build: it only forwards to the neighbouring ``Qiban.exe --uninstall`` (see Program.cs), so every
digest - the build record, the installer identity and the installer's own re-check - stays about the
one published program.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from build_desktop_sources import PUBLISHED_EXE
from build_desktop_sources import verify as verify_desktop_build

MAIN_EXE = 'Qiban.exe'
ALIAS_EXE = 'uninstall.exe'


def validate(directory, entrypoint):
    manifest = json.loads((directory/'runtime-manifest.json').read_text(encoding='utf8'))
    if manifest['format'] != 1 or manifest['entrypoint'] != entrypoint:
        raise ValueError('Wrong resource package')
    for row in manifest['files']:
        file = (directory/row['path']).resolve(strict=True)
        if not file.is_relative_to(directory) or hashlib.sha256(file.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('Resource mismatch')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_alias(directory):
    """The published uninstall entry: one byte-identical copy of the assembled main program.

    No build toolchain is added for it and no second program is published: the copy is refused when
    it is not exactly the main executable, so a stale or hand-edited alias can never ship.
    """
    directory = Path(directory)
    main, alias = directory/MAIN_EXE, directory/ALIAS_EXE
    if not main.is_file():
        raise ValueError('缺少已组装的 Qiban.exe，无法生成卸载入口')
    expected = digest(main)
    shutil.copyfile(main, alias)
    if digest(alias) != expected:
        alias.unlink()
        raise ValueError('卸载入口与主程序不是同一份程序文件')
    return alias


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['desktop','storage','postgres','output']:
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--qq-runtime', type=Path)
    parser.add_argument('--wechat-runtime', type=Path)
    parser.add_argument('--speech', type=Path)
    args = parser.parse_args()
    if json.loads((args.desktop/'managed-application.json').read_text(encoding='utf-8-sig')) != {'mode':'managed','format':1}:
        raise ValueError('Use build.ps1 -ManagedApplication for the desktop input')
    desktop = args.desktop.resolve(strict=True)
    storage, postgres = args.storage.resolve(strict=True), args.postgres.resolve(strict=True)
    validate(storage,'Qiban.Storage.exe')
    validate(postgres,'bin/postgres.exe')
    # The input shell is checked before any output exists, so a bad package leaves nothing behind.
    verify_desktop_build(desktop, desktop/PUBLISHED_EXE)
    output = args.output.resolve()
    shutil.copytree(desktop,output)
    (output/PUBLISHED_EXE).rename(output/MAIN_EXE)
    verify_desktop_build(output, output/MAIN_EXE)
    write_alias(output)
    shutil.copytree(storage,output/'resources/storage')
    shutil.copytree(postgres,output/'resources/postgres')
    if args.speech is not None:
        speech = args.speech.resolve(strict=True)
        validate(speech, 'Qiban.Speech.exe')
        shutil.copytree(speech, output/'resources/speech')
    if args.wechat_runtime is not None:
        wechat = args.wechat_runtime.resolve(strict=True)
        validate(wechat, 'Qiban.WeChatReader.exe')
        shutil.copytree(wechat, output/'resources/wechat')
    if args.qq_runtime is not None:
        from companion_qq.native_resources import prepare_local_runtime, verify_runtime

        verify_runtime(args.qq_runtime)
        prepare_local_runtime(args.qq_runtime, output/'resources/qq')
    (output/'README.txt').write_text(
        '栖伴 Qiban\n'
        '双击 Qiban.exe 启动；首次启动会在个人资料目录内完成初始化，并自动准备本机后台（数据库与加密资料后台）。\n'
        '右上角「应用设置」可以启动或停止应用后台、收至托盘、打开应用使用说明、打开卸载准备和退出应用。\n'
        '关闭窗口只会收至托盘，双击托盘图标即回到原窗口。\n'
        '使用模型前请在本机安装 Codex，并在「模型与用量」中登录 ChatGPT 账号；不需要创建 Codex 聊天或填写 API Key。\n'
        'QQ、微信与 B 站请在「平台与会话」中配置；微信还需保持电脑微信登录。登录后按需授权会话并启动平台。\n'
        '安装、升级与卸载请使用配套安装包；卸载请从开始菜单的「卸载栖伴」、Windows 应用列表或本目录的\n'
        'uninstall.exe 进入卸载准备页，先保存备份或明确跳过，再确认卸载。\n'
        '免安装目录可先备份，再退出应用并手动删除程序文件夹。\n'
        '个人资料、备份与凭据使用独立目录保存，卸载程序会保留它们。自己导出的备份请放在程序目录之外。\n'
        '必须保留整个应用目录，不能只复制 EXE。\n'
        'desktop-build.json 与 desktop-sources.zip 记录本包桌面外壳的源码与清单。\n',
        encoding='utf8')
    print(json.dumps({'application':str(output/MAIN_EXE),'alias':str(output/ALIAS_EXE),
                      'bytes':sum(p.stat().st_size for p in output.rglob('*') if p.is_file())}))


if __name__ == '__main__':
    main()
