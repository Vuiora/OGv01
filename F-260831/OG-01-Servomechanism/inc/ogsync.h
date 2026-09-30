#ifndef __OGSYNC_H
#define __OGSYNC_H

#include <semaphore.h>
#ifdef __cplusplus
extern "C" {
#endif

//返回linux信号量，写一个函数使其能够实现两台设备之间的数据同步


sem_t *ogsync(const char *name, unsigned int initial_value);
int ogsync_wait(sem_t *semaphore);
int ogsync_post(sem_t *semaphore);
int ogsync_close(sem_t *semaphore);
int ogsync_unlink(const char *name);

#ifdef __cplusplus
}
#endif

#endif
