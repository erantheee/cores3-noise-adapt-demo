/// 在开发机上编译导出头文件，验证 C 代码路径与 numpy 参考实现一致。
///
/// 为什么要有这一步：导出物是"生成的 C 数组 + 手写的索引代码"，索引写错在设备上不会报错，
/// 只会安静地给出错误决策。先在主机上把它编译、跑到与期望标签一致，端侧才值得去刷。
///
/// 编译（与 cores3-noise-adapt-demo 的 C++ 单测同款）：
///   c++ -std=c++17 -O2 -I <artifacts 目录> host_check.cpp -o /tmp/host_check && /tmp/host_check

#include <cstdio>

#include "listening_model.h"
#include "test_vectors.h"

int main() {
    int correct = 0;
    for (int i = 0; i < test_vectors::kCount; ++i) {
        float logits[listening_model::kClassCount];
        listening_model::classify(test_vectors::kData[i], logits);
        int best = 0;
        for (std::size_t c = 1; c < listening_model::kClassCount; ++c) {
            if (logits[c] > logits[best]) best = static_cast<int>(c);
        }
        if (best == test_vectors::kExpected[i]) ++correct;
        std::printf("%d %d %d\n", i, test_vectors::kExpected[i], best);
    }
    std::printf("correct %d/%d\n", correct, test_vectors::kCount);
    return correct == test_vectors::kCount ? 0 : 1;
}
