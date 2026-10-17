"""Valid locally generated sample files for clearly labelled demonstrations."""

import io
import zipfile


def make_demo_files():
    notes = ("这是下载工作台的本机演示文件，不是真实客户资料。\n" * 18000).encode(
        "utf-8"
    )
    table = (
        "序号,内容\n"
        + "".join(f"{index},本机生成的示例数据\n" for index in range(32000))
    ).encode("utf-8-sig")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("说明.txt", notes)
    return {
        "使用说明.txt": notes,
        "示例数据.csv": table,
        "演示文件.zip": buffer.getvalue(),
    }
