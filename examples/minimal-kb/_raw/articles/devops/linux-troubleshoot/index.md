# Linux 排障速查

## 磁盘空间不足

```bash
# 查看磁盘使用
df -h

# 找大文件
du -sh /* | sort -rh | head -20

# 清理 Docker
docker system prune -a

# 清理日志
journalctl --vacuum-time=7d
```

## 端口被占用

```bash
# 查看端口占用
lsof -i :8080
ss -tlnp | grep 8080

# 杀掉进程
kill -9 $(lsof -t -i :8080)
```

## 内存不足

```bash
# 查看内存
free -h

# 查看进程内存
ps aux --sort=-%mem | head -10

# 清理缓存
echo 3 > /proc/sys/vm/drop_caches
```

## SSH 连接超时

```bash
# 检查 sshd 状态
systemctl status sshd

# 查看认证日志
tail -f /var/log/auth.log

# 检查防火墙
iptables -L -n | grep 22
```
