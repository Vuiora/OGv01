#include <unistd.h>
#include <fcntl.h>
#include <stdlib.h>
#include "stdio.h" 

#define BUFFERSIZE 4096

char buffer1[] = "abcdefg";
char buffer2[] = "ABCDEFG";
char buffer3[10000];
int main(){
    int fd;
    if((fd = open("TESTORFILE",O_CREAT | O_RDWR,0644)) <0){
        printf("ERROR LSEEK!\n");
        exit(-1);
    }
    if(write(fd,buffer1,7) < 0){
        printf("ERROR WRITE!\n");
        exit(-3);
    }
    if(lseek(fd,0x00010000,SEEK_END)<0){
        printf("ERROR LSEEK!\n");
        exit(-2);
    }
    if(write(fd,buffer2,7) < 0){
        printf("ERROR WRITE!\n");
        exit(-3);
    }
    printf("RUN SUCESSFULLY!\n");
    read(fd,buffer3,BUFFERSIZE);
    printf("%c\n",buffer3[0]);
    close(fd);
    return 0;
}